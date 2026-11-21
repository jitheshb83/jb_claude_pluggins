#!/usr/bin/env python3
"""PreToolUse gate for jb-sdlc-plugin — the thin, early-warning layer.

Enforcement lives where the command's spelling doesn't matter (see README,
"Threat model"): the session's git hooks (push, commit, base-branch moves),
drift detection after every Bash call, and state-integrity checks. This gate
only adds what those can't see before a tool runs:

Edit/Write/MultiEdit/NotebookEdit
  docs/sdlc/*/state.json        -> always refused; only sdlc.py writes approvals
  code outside docs/sdlc        -> refused until the branch's spec+plan are approved
Bash (pattern checks, not a shell parser)
  gh pr create|merge, gh api writes to pulls/merges, git push
                                -> refused: the SDLC never publishes (SDLC_ALLOW_PUSH=1 lifts)
  switching the git hooks off   -> refused: --no-verify, core.hooksPath, GIT_CONFIG_*,
                                   CLAUDECODE/SDLC_* assignments, env -i/-u
  git commit                    -> refused on main/master or a merged branch, and when it
                                   would commit code before plan approval

Exit 0 allows, exit 2 refuses (stderr goes to Claude). SDLC_GATES=off disables.
On an internal error it refuses git/gh commands and allows everything else.
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
_RAW = ""

# `<<` that opens a heredoc — not `<<<`, not a shift inside $((...)).
HEREDOC = re.compile(r"(?<![<])<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
# Text that runs other text: quoted payloads here are code, not data.
RUNS_TEXT = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?(?:bash|sh|zsh|dash|ksh)\s+-\w*c|\beval\b|\$\(|`"
                       r"|\|\s*(?:\S*/)?(?:bash|sh|zsh|dash|ksh|xargs|source)\b|(?:^|\s)(?:bash|sh|zsh)\s*<<")
GIT = r"(?<![\w.-])[\"']?(?:\S*/)?git[\"']?"
GIT_OPTS = r"(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+\S+|\s+-{1,2}[\w-]+(?:=\S+)?)*"
GIT_SUB = re.compile(GIT + GIT_OPTS + r"\s+(push|commit)(?![\w-])")
GH_PR = re.compile(r"(?<![\w.-])(?:\S*/)?gh\b[^;&|\n]*?\bpr\s+(create|merge)\b")
GH_API_WRITE = re.compile(
    r"(?<![\w.-])(?:\S*/)?gh\b(?=[^;&|\n]*\bapi\b)(?=[^;&|\n]*/(?:pulls|merges)\b)"
    r"(?=[^;&|\n]*(?:(?:-X|--method)[\s=]*(?:POST|PUT|PATCH|DELETE)\b|\s(?:-f|-F|--field|--raw-field|--input)\b))",
    re.I)
# Flag forms: matched on the raw text, since their values are often quoted.

HOOK_FLAGS = re.compile(
    r"\s-c\s*[\"']?core\.hookspath"
    r"|\bconfig\b(?![^;&|\n]*--get)[^;&|\n]*\bcore\.hookspath|--config-env", re.I)
# Environment changes: matched on quote-masked text, so quoted mentions stay data.
HOOK_OFF = re.compile(
    r"(?<![\w.-])(?:CLAUDECODE|SDLC_[A-Z_]+|GIT_CONFIG_\w+)="
    r"|\b(?:unset|export)\b[^;&|\n]*\b(?:CLAUDECODE|SDLC_[A-Z_]+|GIT_CONFIG_\w+)\b"
    r"|(?<![\w.-])env\s+(?:\S+\s+)*?(?:-i\b|--ignore-environment|-u\s*(?:CLAUDECODE|SDLC_)|--unset)",
    re.I)
CLEANS_UNTRACKED = re.compile(GIT + GIT_OPTS + r"\s+(?:clean\b|stash\b[^;&|\n]*\s(?:-u|--include-untracked|-a|--all)\b)")
COMMIT_ALL = re.compile(r"\bcommit\b[^;&|\n]*\s(?:-[A-Za-z]*a[A-Za-z]*|--all)(?:\s|$)")


def block(msg: str) -> None:
    print(f"SDLC gate: {msg}", file=sys.stderr)
    sys.exit(2)


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies that are data (commit messages, file contents).
    Bodies fed to a shell are code and stay; an unterminated opener keeps its lines."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC.search(line)
        if m and line[:m.start()].count("$((") <= line[:m.start()].count("))") \
                and not RUNS_TEXT.search(line):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == m.group(2)), None)
            if end is not None:
                i = end
        i += 1
    return "\n".join(out)


def mask_quotes(text: str) -> str:
    """Same length as text, with quoted contents blanked (escapes respected)."""
    out, q, i = [], None, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and q != "'":
            out.append("xx" if q else text[i:i + 2])
            i += 2
            continue
        if q:  # placeholder, not a space, so a quoted value stays one token
            out.append(c if c == q else "x")
            q = None if c == q else q
        else:
            q = c if c in "'\"" else None
            out.append(c)
        i += 1
    return "".join(out)


def path_block(path: Path) -> str | None:
    """Why an Edit/Write to `path` must be refused, or None."""
    if ".git" in path.parts and path.name.startswith("sdlc-"):
        return f"{path.name} is SDLC gate state in the git dir; only sdlc.py writes it."
    root = sdlc.repo_root(path)
    if not root:
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    in_sdlc = [p.casefold() for p in rel.parts[:2]] == [p.casefold() for p in sdlc.SDLC_DIR.parts]
    if in_sdlc and rel.name.casefold() == "state.json":
        return (f"{rel} records approvals and is written only by `sdlc.py approve`. "
                f"Approvals come from the user, never from editing state.")
    feat = sdlc.active_feature(root)
    if feat and in_sdlc and rel.name.casefold() in ("plan.md", "spec.md") \
            and sdlc.approval_valid(feat, "spec") and sdlc.approval_valid(feat, "plan"):
        sdlc.save_baseline(root, feat, approved=False)  # editing re-opens the gate: build work isn't drift
    if not feat or in_sdlc:
        return None
    return approval_block(root, feat, f"code edits ({rel})")


def approval_block(root: Path, feat: dict, what: str) -> str | None:
    where = feat["_dir"].relative_to(root)
    if feat.get("_corrupt"):
        return (f"{where}/state.json is unreadable or was changed outside sdlc.py. No {what} until "
                f"the user fixes it by hand; tell them which file.")
    if feat.get("_missing"):
        return (f"the SDLC folder {where} was deleted. No {what} until the user restores it "
                f"(`git checkout HEAD -- {where}`) or abandons the feature (`sdlc.py abandon --note`).")
    if sdlc.approval_valid(feat, "spec") and sdlc.approval_valid(feat, "plan"):
        return None
    ph = sdlc.phase(feat)
    stale = " (it was approved, but the file changed since — re-approve it)" \
        if feat.get("approvals", {}).get(ph) else ""
    return (f"'{feat['title']}' is still in the {ph} phase{stale}. No {what} until the user "
            f"approves {where}/{sdlc.ARTIFACT.get(ph, 'plan.md')} — run the sdlc-{ph} skill.")


def gate_bash(command: str, cwd: Path) -> None:
    allow_push = os.environ.get("SDLC_ALLOW_PUSH", "").lower() in ("1", "true", "yes")
    text = strip_heredocs(command)
    # Quoted text is data (grep patterns, -m messages) unless the command runs text.
    scan = text if RUNS_TEXT.search(text) else mask_quotes(text)
    found = {m.group(1) for m in GIT_SUB.finditer(scan)}

    try:  # exact shell words: a quoted "--no-verify" is still a flag, a -m message isn't
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    no_verify = any(w.startswith("--no-v") and "--no-verify".startswith(w.split("=")[0]) for w in words)
    if HOOK_OFF.search(scan) or HOOK_FLAGS.search(text) or no_verify:
        block("refused: this would switch the SDLC git hooks or gates off (--no-verify, "
              "core.hooksPath, GIT_CONFIG_*, CLAUDECODE / SDLC_* changes, env -i). Only the user "
              "changes those, before the session starts.")
    if not allow_push:
        if "push" in found:
            block("`git push` refused — the SDLC never pushes. Hand the user the push command "
                  "(`! SDLC_ALLOW_PUSH=1 git push ...` in Claude Code, or their own terminal).")
        pr = GH_PR.search(scan)
        if pr or GH_API_WRITE.search(scan):
            block(f"`gh {'pr ' + pr.group(1) if pr else 'api'}` refused — the SDLC never opens or "
                  f"merges PRs. Hand the user the PR body file and the command.")

    root = sdlc.repo_root(cwd)
    if root and CLEANS_UNTRACKED.search(scan):
        untracked = sdlc.git(root, "ls-files", "--others", "--exclude-standard", "--", str(sdlc.SDLC_DIR))
        if untracked:
            block(f"refused: this would delete uncommitted SDLC docs ({untracked.splitlines()[0]}). "
                  f"Commit docs/sdlc first.")
    if re.search(GIT + r"\s+-C\b", scan):
        root = None  # another repo: its own git hooks check the commit
    if root and "commit" in found:
        feat = sdlc.active_feature(root)
        reason = sdlc.check_branch(root, (feat or {}).get("base"))
        if reason:
            block(f"`git commit` refused — {reason}")
        if feat:
            changed = (sdlc.git(root, "diff", "--cached", "--name-only") or "").splitlines()
            if COMMIT_ALL.search(scan):
                changed += (sdlc.git(root, "diff", "--name-only") or "").splitlines()
            code = [p for p in changed if Path(p).parts[:2] != sdlc.SDLC_DIR.parts]
            reason = approval_block(root, feat, f"code commits ({', '.join(code[:3])})") if code else None
            if reason:
                block(f"`git commit` refused — {reason}")


def main() -> None:
    global _RAW
    if os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    _RAW = sys.stdin.read()
    try:
        event = json.loads(_RAW)
    except ValueError:
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
    except Exception as e:
        try:
            cmd = (json.loads(_RAW or "{}").get("tool_input") or {}).get("command", "")
        except ValueError:
            cmd = ""
        if re.search(r"(?<![\w.-])(?:\S*/)?(?:git|gh)\b", cmd or ""):
            print(f"SDLC gate: internal error on a git/gh command, refusing to be safe: {e}",
                  file=sys.stderr)
            sys.exit(2)
        print(f"sdlc gate internal error (allowing): {e}", file=sys.stderr)
        sys.exit(0)
