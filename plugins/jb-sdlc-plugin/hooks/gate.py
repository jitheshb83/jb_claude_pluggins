#!/usr/bin/env python3
"""PreToolUse gate for jb-sdlc-plugin.

Bash:
  git push, gh pr create/merge     -> always blocked: the SDLC never publishes;
                                      the user pushes (SDLC_ALLOW_PUSH=1 lifts it)
  git merge on main/master         -> blocked (lane merges into a feature branch are fine)
  git commit                       -> blocked on main/master or a merged branch
  shell writes (>, tee, sed -i, cp, mv, rm, touch, patch, git apply)
                                   -> same plan gate as Edit/Write below
Edit/Write/MultiEdit/NotebookEdit:
  docs/sdlc/*/state.json           -> always blocked; only sdlc.py writes approvals
  code outside docs/sdlc           -> blocked until the branch's spec+plan are approved

Exit 0 = allow, exit 2 = block (stderr goes to Claude). Internal errors allow.
SDLC_GATES=off disables everything. Not covered: writes from inside
interpreters (python -c, node -e) — the skills forbid them before approval.
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
WRAPPERS = {"sudo", "env", "command", "nohup", "time", "exec", "builtin", "nice", "xargs"}
SHELLS = {"bash", "sh", "zsh"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([\w.-]+)\1")
REDIRECT_OP = re.compile(r"(?:^|[^<>&\w])(?:\d?>>?|&>>?)\s*")
# Fail-closed: anywhere in the command, quoted payloads included (bash -c '...',
# "$(...)", timeout/nice/sudo prefixes, `&` chains). Heredoc bodies are skipped.
GIT_OPTS = r"(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+\S+|\s+-{1,2}[\w-]+(?:=\S+)?)*"
ANY_GIT = re.compile(r"(?<![\w./-])git" + GIT_OPTS + r"\s+(push|commit|merge)\b")
ANY_GH_PR = re.compile(r"(?<![\w./-])gh\s+pr\s+(create|merge)\b")
ALL_ARGS = {"rm", "touch", "mkdir", "rmdir", "truncate", "tee"}
LAST_ARG = {"cp", "mv", "install", "ln", "rsync"}


def block(msg: str) -> None:
    print(f"SDLC gate: {msg}", file=sys.stderr)
    sys.exit(2)


def strip_heredocs(cmd: str) -> str:
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        out.append(lines[i])
        m = HEREDOC.search(lines[i])
        if m:
            i += 1
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
        i += 1
    return "\n".join(out)


def mask_quotes(text: str) -> str:
    """Same length as text, with the contents of quoted strings blanked out."""
    out, q = [], None
    for c in text:
        if q:
            out.append(c if c == q else " ")
            if c == q:
                q = None
        else:
            if c in "'\"":
                q = c
            out.append(c)
    return "".join(out)


def split_commands(cmd: str) -> list[str]:
    """Split on && || ; | & newline $( ` — but only outside quotes."""
    out, cur, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur.append(c)
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
        elif c in "'\"":
            q = c
            cur.append(c)
        elif cmd.startswith(("&&", "||", "$("), i):
            out.append("".join(cur))
            cur = []
            i += 1
        elif c in ";|\n`" or (c == "&" and cmd[i - 1:i] not in ("<", ">")
                                and cmd[i + 1:i + 2] != ">"):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def segments(command: str, cwd: Path):
    """Yield (dir, tokens, raw) per simple command, following cd, subshells,
    wrappers (sudo/env/...) and `bash -c` payloads."""
    here = [cwd]

    def walk(cmd: str):
        for seg in split_commands(strip_heredocs(cmd)):
            raw = seg.strip().lstrip("({ ").rstrip(")} ")
            try:
                toks = shlex.split(raw)
            except ValueError:
                toks = raw.split()
            while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in WRAPPERS
                            or (toks[0].startswith("-") and len(toks) > 1)):
                toks = toks[1:]
            if not toks:
                continue
            if toks[0] == "cd" and len(toks) > 1:
                here[0] = (here[0] / os.path.expanduser(toks[1])).resolve()
                continue
            if toks[0] in SHELLS and "-c" in toks[:-1]:
                yield from walk(toks[toks.index("-c") + 1])
                continue
            yield here[0], toks, raw

    yield from walk(command)


def parse_git(here: Path, toks: list[str]):
    d, i = here, 1
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            d = (here / os.path.expanduser(toks[i + 1])).resolve()
            i += 2
        elif toks[i] in ("-c", "--git-dir", "--work-tree") and i + 1 < len(toks):
            i += 2
        else:
            i += 1
    return d, (toks[i] if i < len(toks) else ""), toks[i + 1:]


def redirect_targets(raw: str) -> list[str]:
    """Redirect targets, ignoring any `>` that sits inside a quoted string."""
    out = []
    for m in REDIRECT_OP.finditer(mask_quotes(raw)):
        rest = raw[m.end():]
        if rest[:1] in ("'", '"'):
            end = rest.find(rest[0], 1)
            t = rest[1:end] if end > 0 else rest[1:]
        else:
            t = re.split(r"[\s;&|<>()]", rest, 1)[0]
        if t and not t.startswith(("&", "/dev/")):
            out.append(t)
    return out


def write_targets(toks: list[str], raw: str) -> list[str]:
    targets = redirect_targets(raw)
    cmd, args = toks[0], [t for t in toks[1:] if not t.startswith("-")]
    if cmd in ALL_ARGS:
        targets += args
    elif cmd in LAST_ARG and args:
        targets.append(args[-1])
    elif cmd in ("sed", "gsed", "perl") and any(t.startswith("-i") or t == "-pi" for t in toks):
        targets += args[1:] if cmd != "perl" else args[-1:]
    elif cmd == "patch" or (cmd == "git" and len(toks) > 1 and toks[1] == "apply"):
        targets.append(".")
    return targets


def path_block(path: Path) -> str | None:
    """Reason a write to `path` must be refused, or None."""
    root = sdlc.repo_root(path)
    if not root:
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    in_sdlc = rel.parts[:2] == sdlc.SDLC_DIR.parts
    if in_sdlc and rel.name == "state.json":
        return (f"{rel} records approvals and is written only by `sdlc.py approve`. "
                f"Approvals come from the user, never from editing state.")
    feat = sdlc.active_feature(root)
    if not feat or in_sdlc:
        return None
    if not sdlc.approval_valid(feat, "spec") or not sdlc.approval_valid(feat, "plan"):
        ph = sdlc.phase(feat)
        stale = " (it was approved, but the file changed since — re-approve it)" \
            if feat.get("approvals", {}).get(ph) else ""
        return (f"'{feat['title']}' is still in the {ph} phase{stale}. No code changes "
                f"({rel}) until the user approves {feat['_dir'].relative_to(root)}/plan.md — "
                f"run the sdlc-{ph} skill.")
    return None


def gate_bash(command: str, cwd: Path) -> None:
    allow_push = os.environ.get("SDLC_ALLOW_PUSH", "").lower() in ("1", "true", "yes")
    text = strip_heredocs(command)
    found = {m.group(1) for m in ANY_GIT.finditer(text)}
    if not allow_push and "push" in found:
        block("`git push` refused — the SDLC never pushes. Tell the user the branch is "
              "ready and give them the exact push command to run themselves.")
    gh = ANY_GH_PR.search(text)
    if gh and not allow_push:
        block(f"`gh pr {gh.group(1)}` refused — the SDLC never publishes or merges. "
              f"Hand the user the PR body file and the command to run themselves.")
    parsed: set[str] = set()
    for here, toks, raw in segments(command, cwd):
        if toks[0] == "git":
            d, sub, args = parse_git(here, toks)
            parsed.add(sub)
            root = sdlc.repo_root(d)
            if sub == "push":
                for arg in args:
                    target = arg.split(":")[-1].lstrip("+").removeprefix("refs/heads/")
                    if target in sdlc.BASE_BRANCHES and not arg.startswith("-"):
                        block(f"`git push` to '{target}' refused — base branches change only via a PR.")
            if root and sub == "merge" and "--abort" not in args:
                cur = sdlc.current_branch(root)
                if cur in sdlc.BASE_BRANCHES or cur == sdlc.default_base(root):
                    block(f"`git merge` into '{cur}' refused — the SDLC never merges into the base; "
                          f"the user merges the PR.")
            if root and sub in ("commit", "push"):
                feat = sdlc.active_feature(root)
                reason = sdlc.check_branch(root, (feat or {}).get("base"))
                if reason:
                    block(f"`git {sub}` refused — {reason}")
        for t in write_targets(toks, raw):
            reason = path_block((here / os.path.expanduser(t)).resolve())
            if reason:
                block(f"shell write refused — {reason}")
    # A commit/merge the segment parser couldn't place (wrapped, backgrounded,
    # nested): check it against the session's repo rather than letting it through.
    root = sdlc.repo_root(cwd)
    if root and found & {"commit", "merge"} - parsed:
        cur = sdlc.current_branch(root)
        if "merge" in found - parsed and (cur in sdlc.BASE_BRANCHES or cur == sdlc.default_base(root)):
            block(f"`git merge` into '{cur}' refused — the SDLC never merges into the base.")
        feat = sdlc.active_feature(root)
        reason = sdlc.check_branch(root, (feat or {}).get("base"))
        if reason:
            block(f"`git commit` refused — {reason}")


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
            reason = path_block((cwd / os.path.expanduser(fp)).resolve())
            if reason:
                block(reason)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # never wedge the session on a gate bug
        print(f"sdlc gate internal error (allowing): {e}", file=sys.stderr)
        sys.exit(0)
