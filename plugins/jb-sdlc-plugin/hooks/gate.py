#!/usr/bin/env python3
"""PreToolUse gate for jb-sdlc-plugin.

Bash  `git commit` / `git push`  -> blocked on main/master or a merged branch.
Edit/Write/NotebookEdit on code -> blocked while the branch's SDLC feature has
                                   no valid plan approval.

Exit 0 = allow, exit 2 = block (stderr is shown to Claude). Any internal error
allows — a broken gate must never wedge the session. SDLC_GATES=off disables.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sdlc  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEPARATORS = re.compile(r"&&|\|\||;|\||\n")


def block(msg: str) -> None:
    print(f"SDLC gate: {msg}\n(Bypass for this session only if the user asks: SDLC_GATES=off)",
          file=sys.stderr)
    sys.exit(2)


def git_invocations(command: str, cwd: Path):
    """Yield (dir, subcommand, args) for each `git commit|push` in a shell line."""
    here = cwd
    for seg in SEPARATORS.split(command):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        while toks and re.match(r"^\w+=", toks[0]):  # FOO=bar git commit
            toks = toks[1:]
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) > 1:
            here = (here / os.path.expanduser(toks[1])).resolve()
            continue
        if toks[0] != "git":
            continue
        d, i = here, 1
        while i < len(toks) and toks[i].startswith("-"):
            if toks[i] == "-C" and i + 1 < len(toks):
                d = (here / os.path.expanduser(toks[i + 1])).resolve()
                i += 2
            elif toks[i] in ("-c", "--git-dir", "--work-tree") and i + 1 < len(toks):
                i += 2
            else:
                i += 1
        if i < len(toks) and toks[i] in ("commit", "push"):
            yield d, toks[i], toks[i + 1:]


def gate_bash(command: str, cwd: Path) -> None:
    for d, sub, args in git_invocations(command, cwd):
        root = sdlc.repo_root(d)
        if not root:
            continue
        feat = sdlc.active_feature(root)
        reason = sdlc.check_branch(root, (feat or {}).get("base"))
        if reason:
            block(f"`git {sub}` refused — {reason}")
        if sub == "push":
            for arg in args:
                target = arg.split(":")[-1].removeprefix("refs/heads/").lstrip("+")
                if target in sdlc.BASE_BRANCHES and not arg.startswith("-"):
                    block(f"`git push` to '{target}' refused — ship via a PR instead.")


def gate_edit(file_path: str, cwd: Path) -> None:
    path = (cwd / os.path.expanduser(file_path)).resolve()
    root = sdlc.repo_root(path)
    if not root:
        return
    feat = sdlc.active_feature(root)
    if not feat:
        return  # branch isn't running the SDLC — nothing to enforce
    try:
        rel = path.relative_to(root)
    except ValueError:
        return
    if rel.parts[:2] == sdlc.SDLC_DIR.parts:
        return  # spec/plan/review themselves are always editable
    if not sdlc.approval_valid(feat, "spec") or not sdlc.approval_valid(feat, "plan"):
        ph = sdlc.phase(feat)
        stale = " (it was approved, but the file changed since — re-approve it)" \
            if feat.get("approvals", {}).get(ph) else ""
        block(f"'{feat['title']}' is still in the {ph} phase{stale}. Code edits "
              f"({rel}) wait until {feat['_dir'].relative_to(root)}/plan.md is approved — "
              f"run the sdlc-{ph} skill.")


def main() -> None:
    if os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or os.getcwd())
    if tool == "Bash":
        gate_bash(inp.get("command", ""), cwd)
    elif tool in EDIT_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path")
        if fp:
            gate_edit(fp, cwd)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # never wedge the session on a gate bug
        print(f"sdlc gate internal error (allowing): {e}", file=sys.stderr)
        sys.exit(0)
