#!/usr/bin/env python3
"""PostToolUse(Bash): catch code changes made before the plan is approved.

The PreToolUse gate predicts writes from command text, which can't be complete.
This checks the outcome instead: files outside docs/sdlc that changed since the
feature started (`sdlc.py new` records a baseline). It tells Claude to revert
them; the pre-commit git hook refuses to commit them either way. Never blocks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sdlc  # noqa: E402


def main() -> None:
    if os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    root = sdlc.repo_root(Path(event.get("cwd") or os.getcwd()))
    feat = sdlc.active_feature(root) if root else None
    if not feat or feat.get("_corrupt") or feat.get("_missing"):
        return
    if sdlc.approval_valid(feat, "spec") and sdlc.approval_valid(feat, "plan"):
        return
    changed = sdlc.drift(root, feat)
    if changed:
        shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
        msg = (f"[jb-sdlc] Code changed before '{feat['title']}' has an approved plan: {shown}. "
               f"Revert these changes now (git checkout -- <file> / delete new files) and continue "
               f"the plan phase; the pre-commit hook will refuse to commit them.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
