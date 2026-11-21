"""Ask-before-publish for hooks/gate.py: a plain feature-branch `git push` or `gh pr create`
becomes a permission prompt (and a push writes a one-shot token for the pre-push hook).
Anything not confidently classified returns, so the gate's refusals apply."""
from __future__ import annotations

import json
import re
import shlex
import sys
import time
from pathlib import Path

import sdlc

SHELL_META = re.compile(r"[;&|<>`$()\\\n\r]")
DQ_META = re.compile(r"[`$\\\n\r]")  # still run inside double quotes; ( ) ; & | < > are literal there
PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "-n", "--dry-run"}


def has_shell_meta(command: str) -> bool:
    """True if the command could run more than one plain command. Single quotes are literal,
    double quotes still expand $ ` and \\; an unbalanced quote counts as unsure."""
    quote = ""
    for ch in command:
        if ch in "\n\r":
            return True
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif DQ_META.match(ch):
                return True
        elif ch in "'\"":
            quote = ch
        elif SHELL_META.match(ch):
            return True
    return bool(quote)


def ask_commit(command: str) -> None:
    """Ask the user for a plain `git commit`; returns for anything not confidently plain."""
    if has_shell_meta(command):
        return
    try:
        words = shlex.split(command)
    except ValueError:
        return
    if words[:2] == ["git", "commit"]:
        ask("`git commit` will record a commit on this branch.")


def ask_publish(command: str, cwd: Path) -> None:
    """Ask the user (permission prompt) for a plain push of a feature branch or `gh pr create`.
    Returns for anything else, so the refusals below apply. Unsure means refuse."""
    if has_shell_meta(command):
        return
    try:
        words = shlex.split(command)
    except ValueError:
        return
    if words[:3] == ["gh", "pr", "create"]:
        ask("`gh pr create` will open a pull request.")
    if words[:2] != ["git", "push"]:
        return
    root = sdlc.repo_root(cwd)
    if not root:
        return
    pos = []
    for w in words[2:]:
        if w.startswith("-"):
            if w not in PUSH_FLAGS and not (not w.startswith("--") and set(w[1:]) <= set("uqvn")):
                return
        else:
            pos.append(w)
    feat = sdlc.active_feature(root)
    bases = sdlc.BASE_BRANCHES | {sdlc.default_base(root)} | ({feat["base"]} if feat and feat.get("base") else set())
    branch = sdlc.current_branch(root)
    remote, specs = (pos[0], pos[1:]) if pos else ("the default remote", [])
    pushes = []
    for spec in specs or ["HEAD"]:
        src, _, dst = spec.partition(":")
        if spec.startswith(("+", ":")) or not src or (":" in spec and not dst):
            return
        dst = dst or src
        dst = branch if dst == "HEAD" else dst.removeprefix("refs/heads/")
        sha = sdlc.git(root, "rev-parse", "--verify", "--quiet", f"{src}^{{commit}}")
        if not dst or dst.startswith(("refs/", "-")) or dst in bases or not sha:
            return
        pushes.append({"ref": f"refs/heads/{dst}", "sha": sha})
    path = sdlc.push_token_path(root)
    if not path:
        return
    path.write_text(json.dumps({"pushes": pushes, "expires": time.time() + sdlc.PUSH_TOKEN_TTL}))
    ask(f"`git push` will publish {', '.join(p['ref'].removeprefix('refs/heads/') for p in pushes)} "
        f"to {remote}.")


def ask(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)
