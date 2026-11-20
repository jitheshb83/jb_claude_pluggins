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
_RAW = ""
TREE = "\0tree:"    # + repo dir: "rewrites tracked files" (merge, apply, stash pop, ...)
CLEAN = "\0clean:"  # + repo dir: "deletes untracked files" (git clean, stash -u)
# Prefix commands that run another command, with the options that take a value.
WRAPPERS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D"}, "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"}, "gtimeout": {"-s", "-k"},
    "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s"}, "stdbuf": set(), "taskset": set(),
    "caffeinate": {"-t", "-w"}, "unbuffer": set(), "chronic": set(), "flock": {"-w", "-E"},
    "command": set(), "nohup": set(), "time": set(), "exec": {"-a"}, "builtin": set(),
}
NUMERIC_ARG = {"timeout", "gtimeout", "nice", "taskset", "flock"}  # take a bare value
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# `<<` that opens a heredoc — not `<<<` (here-string) and not a shift inside $((...)).
HEREDOC = re.compile(r"(?<![<])<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
REDIRECT_OP = re.compile(r"(?<![<>&=-])(?:\d?>>?|&>>?)\|?\s*")
# Fail-closed: anywhere in the command, quoted payloads included (bash -c '...',
# "$(...)", timeout/nice/sudo prefixes, `&` chains). Heredoc bodies are skipped.
GIT_OPTS = r"(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+\S+|\s+-{1,2}[\w-]+(?:=\S+)?)*"
ANY_GIT = re.compile(r"(?<![\w.-])[\"']?git[\"']?" + GIT_OPTS + r"\s+(push|commit|merge)\b")
ANY_GH_PR = re.compile(r"(?<![\w.-])[\"']?gh[\"']?(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+pr\s+(create|merge)\b")
GH_VALUE_OPTS = {"-R", "--repo", "--hostname"}
GH_API_PUBLISH = re.compile(r"(?<![\w.-])gh\b[^;&|\n]*\bapi\b[^;&|\n]*/(?:pulls|merges)\b(?![^;&|\n]*(?:-X|--method)\s*GET)")
ENV_EVASION = re.compile(
    r"(?<![\w.-])(?:CLAUDECODE|SDLC_[A-Z_]+)=|\bunset\b[^;&|\n]*\b(?:CLAUDECODE|SDLC_)"
    r"|(?<![\w.-])env\s+(?:-\S*\s+)*-(?:i\b|-ignore-environment|u\s*(?:CLAUDECODE|SDLC_|GIT_CONFIG))")
HOOK_EVASION = re.compile(r"--no-verify\b|\bcore\.hooksPath\b|\bGIT_CONFIG_(?:COUNT|KEY|VALUE|PARAMETERS|GLOBAL|SYSTEM|NOSYSTEM)\b|\bSDLC_GITHOOKS\b")
ALL_ARGS = {"rm", "touch", "mkdir", "rmdir", "truncate", "tee", "mv"}
DEST_ARG = {"cp", "install", "ln", "rsync"}
GIT_PATH_SUBS = {"rm", "mv", "checkout", "restore"}
GIT_TREE_REWRITES = {"cherry-pick", "am", "revert", "merge", "pull", "rebase"}  # rewrite tracked files
GIT_SUBCOMMANDS = GIT_PATH_SUBS | GIT_TREE_REWRITES | {
    "add", "apply", "bisect", "blame", "branch", "clone", "commit", "config", "describe", "diff",
    "fetch", "grep", "init", "log", "ls-files", "ls-tree", "notes", "push", "reflog", "remote",
    "reset", "rev-list", "rev-parse", "shortlog", "show", "stash", "status", "switch", "tag",
    "worktree", "cat-file", "hash-object", "symbolic-ref", "for-each-ref", "help", "version",
    "clean", "gc", "submodule", "sparse-checkout", "format-patch", "range-diff", "merge-base"}


def block(msg: str) -> None:
    print(f"SDLC gate: {msg}", file=sys.stderr)
    sys.exit(2)


SHELL_FED = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|sh|zsh|dash|ksh|eval|xargs|ssh)\b[^|;&]*$")


def _heredoc_start(line: str):
    for m in HEREDOC.finditer(line):
        before = line[:m.start()]
        if before.count("$((") > before.count("))"):
            continue  # arithmetic shift, e.g. $((a<<b))
        if SHELL_FED.search(before):
            continue  # `bash <<EOF`: the body runs, so keep it visible to the checks
        return m
    return None


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (commit messages, file contents). An opener with no
    closing delimiter isn't a heredoc, so its following lines are kept."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        out.append(lines[i])
        m = _heredoc_start(lines[i])
        if m:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == m.group(2)), None)
            if end is not None:
                i = end
        i += 1
    return "\n".join(out)


def mask_quotes(text: str) -> str:
    """Same length as text, with the contents of quoted strings blanked out."""
    out, q, i = [], None, 0
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\" and q == '"' and i + 1 < len(text):
                out.append("  ")
                i += 2
                continue
            out.append(c if c == q else " ")
            if c == q:
                q = None
        elif c == "\\" and i + 1 < len(text):
            out.append(c + text[i + 1])  # escaped char outside quotes opens nothing
            i += 2
            continue
        else:
            if c in "'\"":
                q = c
            out.append(c)
        i += 1
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
        elif c == "\\" and i + 1 < len(cmd):
            cur.append(c + cmd[i + 1])  # an escaped quote or separator is literal
            i += 2
            continue
        elif c in "'\"":
            q = c
            cur.append(c)
        elif cmd.startswith(("&&", "||", "$("), i):
            out.append("".join(cur))
            cur = []
            i += 1
        elif (c in ";\n`" or (c == "|" and cmd[i - 1:i] != ">")
              or (c == "&" and cmd[i - 1:i] not in ("<", ">") and cmd[i + 1:i + 2] != ">")):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def base(tok: str) -> str:
    return os.path.basename(tok)


def strip_wrappers(toks: list[str]) -> list[str]:
    """Drop VAR=val assignments and wrapper commands (with their options and
    values) so toks[0] is the command that actually runs."""
    while toks:
        if re.match(r"^\w+=", toks[0]):
            toks = toks[1:]
            continue
        w = base(toks[0])
        if w not in WRAPPERS:
            return toks
        toks, took_value = toks[1:], False
        while toks:
            t = toks[0]
            if t == "--":
                toks = toks[1:]
                break
            if t.startswith("-"):
                toks = toks[2:] if t in WRAPPERS[w] else toks[1:]
            elif w == "env" and "=" in t:
                toks = toks[1:]
            elif w in NUMERIC_ARG and not took_value and re.fullmatch(r"[\d.,x-]+[smhd]?", t):
                toks, took_value = toks[1:], True
            else:
                break
    return toks


PROTECTED_ENV = re.compile(r"^(?:CLAUDECODE|SDLC_[A-Z_]+|GIT_CONFIG_\w+)$")


def env_evasion(toks: list[str]) -> bool:
    """True if this simple command sets/unsets a variable the gates depend on."""
    i = 0
    while i < len(toks) and re.match(r"^\w+=", toks[i]):
        if PROTECTED_ENV.match(toks[i].split("=", 1)[0]):
            return True
        i += 1
    rest = toks[i:]
    if not rest:
        return False
    cmd = base(rest[0])
    if cmd in ("export", "unset", "declare", "typeset", "readonly", "local"):
        return any(PROTECTED_ENV.match(a.split("=", 1)[0].lstrip("-")) for a in rest[1:])
    if cmd == "env":
        args = rest[1:]
        for j, a in enumerate(args):
            if a in ("-i", "--ignore-environment") or a.startswith("-i"):
                return True
            if a in ("-u", "--unset") and j + 1 < len(args) and PROTECTED_ENV.match(args[j + 1]):
                return True
            if "=" in a and PROTECTED_ENV.match(a.split("=", 1)[0]):
                return True
    return False


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
            if env_evasion(toks):
                block("refused: the command changes CLAUDECODE / SDLC_* / GIT_CONFIG_* in its "
                      "environment, which would switch the SDLC gates off. Only the user sets those.")
            toks = strip_wrappers(toks)
            if not toks:
                continue
            if toks[0] == "cd" and len(toks) > 1:
                here[0] = (here[0] / os.path.expanduser(toks[1])).resolve()
                continue
            if base(toks[0]) in SHELLS:
                flag = next((i for i, t in enumerate(toks[1:-1], 1)
                             if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t)), None)
                if flag is not None:
                    yield from walk(toks[flag + 1])
                    continue
            if toks[0] == "eval" and len(toks) > 1:
                yield from walk(" ".join(toks[1:]))
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


def _inplace_files(cmd: str, toks: list[str]) -> list[str]:
    """File operands of `sed -i` / `perl -i`, skipping scripts and the BSD suffix."""
    inplace, script_given, files, i = False, False, [], 1
    while i < len(toks):
        t = toks[i]
        if t in ("-e", "-f", "--expression", "--file"):
            script_given, i = True, i + 2
            continue
        if t.startswith("-") and t != "-":
            sed_inplace = cmd != "perl" and (re.fullmatch(r"-[A-Za-z]*i.*", t)
                                             or t == "--in-place" or t.startswith("--in-place="))
            if sed_inplace or (cmd == "perl" and "i" in t.lstrip("-")):
                inplace = True
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                if cmd != "perl" and t == "-i" and nxt is not None and (nxt == "" or nxt.startswith(".")):
                    i += 1  # BSD sed: `-i ''` / `-i .bak`
            if cmd == "perl" and t.endswith("e") and t != "-e":
                script_given, i = True, i + 2  # -pie 'script'
                continue
            i += 1
            continue
        if not script_given:
            script_given = True
        else:
            files.append(t)
        i += 1
    return files if inplace else []


def _dest(dest: str, srcs: list[str], here: Path) -> list[str]:
    d = (here / os.path.expanduser(dest))
    if dest.endswith("/") or d.is_dir():
        return [str(d / os.path.basename(s.rstrip("/"))) for s in srcs] or [dest]
    return [dest]


def _target_dir(toks: list[str]) -> tuple[str | None, list[str]]:
    """(-t DIR | --target-directory=DIR) and the remaining operands."""
    out, tdir, i = [], None, 1
    while i < len(toks):
        t = toks[i]
        if t in ("-t", "--target-directory") and i + 1 < len(toks):
            tdir, i = toks[i + 1], i + 2
            continue
        if t.startswith("--target-directory="):
            tdir = t.split("=", 1)[1]
        elif t and not t.startswith("-"):
            out.append(t)
        i += 1
    return tdir, out


def write_targets(toks: list[str], raw: str, here: Path) -> list[str]:
    targets = redirect_targets(raw)
    cmd, args = base(toks[0]), [t for t in toks[1:] if not t.startswith("-") and t]
    if cmd in ALL_ARGS | DEST_ARG:
        tdir, ops = _target_dir(toks)
        if tdir:
            return targets + (ops if cmd == "mv" else []) + [
                str(Path(tdir) / os.path.basename(o.rstrip("/"))) for o in ops]
    if cmd in ALL_ARGS:
        targets += args
        if cmd == "mv" and len(args) > 1:
            targets += _dest(args[-1], args[:-1], here)
    elif cmd in DEST_ARG and args:
        targets += _dest(args[-1], args[:-1], here)
    elif cmd in ("sed", "gsed", "perl"):
        targets += _inplace_files(cmd, toks)
    elif cmd == "patch":
        targets.append(TREE + str(here))
    elif cmd == "git":
        d, sub, gargs = parse_git(here, toks)
        hard_reset = sub == "reset" and any(f in gargs for f in ("--hard", "--merge", "--keep"))
        stash_out = sub == "stash" and gargs[:1] in (["pop"], ["apply"])
        stash_untracked = sub == "stash" and any(
            f in gargs for f in ("-u", "--include-untracked", "-a", "--all"))
        if sub == "clean" or stash_untracked:
            targets.append(CLEAN + str(d))
        elif sub == "apply" or sub in GIT_TREE_REWRITES or hard_reset or stash_out:
            targets.append(TREE + str(d))
        elif sub in GIT_PATH_SUBS:
            if "--" in gargs:
                ops = [t for t in gargs[gargs.index("--") + 1:] if t]
                whole = sub in ("checkout", "restore") and any(
                    (d / t).resolve() == (sdlc.repo_root(d) or d) for t in ops)
                targets += [TREE + str(d)] if whole else [str(d / t) for t in ops]
            elif sub in ("rm", "mv"):
                targets += [str(d / t) for t in gargs if not t.startswith("-") and t]
            else:  # checkout/restore <path>: only operands that are existing files
                targets += [str(d / t) for t in gargs if not t.startswith("-") and (d / t).is_file()]
    return targets


def path_block(path: Path, held_check: bool = True) -> str | None:
    """Reason a write to `path` must be refused, or None."""
    root = sdlc.repo_root(path)
    if not root:
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    folded = [part.casefold() for part in rel.parts]
    in_sdlc = folded[:2] == [part.casefold() for part in sdlc.SDLC_DIR.parts]
    if in_sdlc and rel.name.casefold() == "state.json":
        return (f"{rel} records approvals and is written only by `sdlc.py approve`. "
                f"Approvals come from the user, never from editing state.")
    held = [f for f in (root / sdlc.SDLC_DIR).glob("*/state.json")
            if held_check and str(path).casefold() in {str(a).casefold() for a in f.parents}]
    if held:
        return (f"{rel} holds SDLC feature state ({held[0].parent.name}); deleting, moving or "
                f"replacing it would bypass the gates. Only sdlc.py manages feature folders.")
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


def gate_bash(command: str, cwd: Path, depth: int = 0) -> None:
    allow_push = os.environ.get("SDLC_ALLOW_PUSH", "").lower() in ("1", "true", "yes")
    text = strip_heredocs(command)
    # Quoted text is data (grep patterns, -m messages) unless it holds a command substitution;
    # quoted payloads that do run (bash -c, eval) are unwrapped by the segment parser below.
    scan = text if ("$(" in text or "`" in text) else mask_quotes(text)
    found = {m.group(1) for m in ANY_GIT.finditer(scan)}
    if ENV_EVASION.search(scan):
        block("refused: the command changes CLAUDECODE / SDLC_* in its environment, which would "
              "switch the SDLC gates off. Only the user sets those, before the session starts.")
    if not allow_push and "push" in found:
        block("`git push` refused — the SDLC never pushes. Tell the user the branch is "
              "ready and give them the exact push command to run themselves.")
    runs_git = re.search(r"(?<![\w.-])[\"']?git[\"']?(?:\s|$)", scan)
    if runs_git and HOOK_EVASION.search(text) or re.search(r"\bunset\b[^;&|\n]*\bGIT_CONFIG", text):
        block("refused: this command would skip or override the SDLC git hooks "
              "(--no-verify, core.hooksPath, GIT_CONFIG_*).")
    if GH_API_PUBLISH.search(scan) and not allow_push:
        block("`gh api` on pulls/merges refused — the SDLC never publishes or merges.")
    gh = ANY_GH_PR.search(scan)
    if gh and not allow_push:
        block(f"`gh pr {gh.group(1)}` refused — the SDLC never publishes or merges. "
              f"Hand the user the PR body file and the command to run themselves.")
    parsed: set[str] = set()
    for here, toks, raw in segments(command, cwd):
        if base(toks[0]) == "gh" and not allow_push:
            rest, i = toks[1:], 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in GH_VALUE_OPTS else 1
            if rest[i:i + 1] == ["pr"] and rest[i + 1:i + 2] in (["create"], ["merge"]):
                block(f"`gh pr {rest[i + 1]}` refused — the SDLC never publishes or merges.")
        if base(toks[0]) == "git":
            d, sub, args = parse_git(here, toks)
            if sub and sub not in GIT_SUBCOMMANDS and depth < 3:
                alias = sdlc.git(d, "config", "--get", f"alias.{sub}")
                if alias:
                    expanded = alias[1:] if alias.startswith("!") else f"git {alias}"
                    gate_bash(expanded + " " + " ".join(shlex.quote(a) for a in args), d, depth + 1)
            parsed.add(sub)
            root = sdlc.repo_root(d)
            if sub == "push" and not allow_push:
                block("`git push` refused — the SDLC never pushes. Tell the user the branch is "
                      "ready and give them the exact push command to run themselves.")
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
            if root and sub in ("commit", "push", "cherry-pick", "revert", "am", "merge", "pull", "rebase") \
                    and "--abort" not in args:
                feat = sdlc.active_feature(root)
                reason = sdlc.check_branch(root, (feat or {}).get("base"))
                if reason:
                    block(f"`git {sub}` refused — {reason}")
        for t in write_targets(toks, raw, here):
            if t.startswith((TREE, CLEAN)):
                d = Path(t.split(":", 1)[1])
                r = sdlc.repo_root(d) or d
                untracked = t.startswith(CLEAN) and sdlc.git(
                    r, "ls-files", "--others", "--exclude-standard", "--", str(sdlc.SDLC_DIR))
                reason = (f"this would delete uncommitted SDLC docs ({untracked.splitlines()[0]}); "
                          f"commit docs/sdlc first or ask the user" if untracked
                          else path_block(r, held_check=False))
            else:
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
            block(f"`git {'/'.join(sorted(found & {'commit', 'merge'} - parsed))}` refused — {reason}")


def main() -> None:
    if os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    global _RAW
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
        # Fail closed only where it matters: a command that commits, pushes, merges
        # or opens a PR. Anything else is allowed so a gate bug can't wedge the session.
        try:
            cmd = (json.loads(_RAW or "{}").get("tool_input") or {}).get("command", "")
        except ValueError:
            cmd = ""
        if ANY_GIT.search(cmd) or ANY_GH_PR.search(cmd) or GH_API_PUBLISH.search(cmd):
            print(f"SDLC gate: internal error on a git/gh command, refusing to be safe: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"sdlc gate internal error (allowing): {e}", file=sys.stderr)
        sys.exit(0)
