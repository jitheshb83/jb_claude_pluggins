#!/usr/bin/env python3
"""UserPromptSubmit router for jb-sdlc-plugin.

Adds one short line of context (plain stdout on exit 0) when either:
  * the current branch has an active SDLC feature  -> its phase, so work resumes
    in the right skill even after /clear, or
  * the prompt reads like a development task        -> a pointer to the sdlc skill.
Otherwise prints nothing, so unrelated prompts cost zero extra tokens.
SDLC_AUTOROUTE=off disables it. Never blocks a prompt.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sdlc  # noqa: E402

# Code-change verbs as an instruction: at the start, after a politeness/ask prefix,
# or as "take/work on this task". The hint is ~50 tokens, so a broad match is cheap.
VERBS = (r"(?:add|implement|build|create|write|make|fix|change|update|modify|refactor|rename|remove|"
         r"delete|migrate|port|support|develop|extend|improve|optimi[sz]e|rewrite|replace|upgrade|ship|plan)")
DEV_INTENT = re.compile(
    r"(?:\A\s*|(?:please|pls|can you|could you|would you|let'?s|i want to|i'd like to|we need to|"
    r"need to|help me|go ahead and)\s+)" + VERBS + r"\b"
    r"|\b(?:take|pick)(?: up| on)? (?:this|the|a|my|next) (?:task|ticket|issue|story)"
    r"|\bwork on (?:this|the|a|issue|ticket|task)",
    re.IGNORECASE,
)
QUESTION_ONLY = re.compile(r"^\s*(what|why|how does|how do|explain|can you explain|is there|does)\b", re.I)


def message(prompt: str, cwd: Path) -> str | None:
    if not prompt or prompt.lstrip().startswith("/"):
        return None  # slash commands route themselves
    root = sdlc.repo_root(cwd)
    feat = sdlc.active_feature(root) if root else None
    if feat and (feat.get("_corrupt") or feat.get("_missing")):
        return (f"[jb-sdlc] SDLC state for '{feat['title']}' is unreadable or missing; code changes "
                f"are blocked. Tell the user to fix or restore {feat['_dir'].name} — don't continue it.")
    if feat:
        return (f"[jb-sdlc] Branch '{feat['branch']}' has an active SDLC feature "
                f"\"{feat['title']}\" ({feat.get('track', 'full')} track), phase: "
                f"{sdlc.phase(feat)}. Continue it via the sdlc skill unless this prompt is unrelated.")
    if DEV_INTENT.search(prompt) and not QUESTION_ONLY.match(prompt):
        where = f" in {root.name}" if root else ""
        return (f"[jb-sdlc] This reads like a development task{where}. The user's standing "
                f"preference is to run development work through the sdlc skill, which sizes it "
                f"(quick / lite / full) and picks models per phase.")
    return None


def main() -> None:
    if os.environ.get("SDLC_AUTOROUTE", "").lower() in ("off", "0", "false"):
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    msg = message(event.get("prompt", ""), Path(event.get("cwd") or os.getcwd()))
    if msg:
        print(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a router bug must never block a prompt
        pass
    sys.exit(0)
