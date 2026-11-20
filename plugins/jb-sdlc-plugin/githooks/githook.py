#!/usr/bin/env python3
"""Git-level SDLC enforcement, active only in Claude Code sessions (CLAUDECODE=1).

Installed per session through core.hooksPath (see hooks/session.py). Git runs
these however the command was typed (aliases, absolute paths, wrappers, heredocs),
which text matching in hooks/gate.py can't guarantee. Every hook then chains to
the repository's own hook of the same name, so existing hooks keep running.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import sdlc  # noqa: E402


def refuse(msg: str) -> None:
    print(f"SDLC (git hook): {msg}", file=sys.stderr)
    sys.exit(1)


def enforce(name: str, root: Path, data: bytes = b"") -> None:
    if os.environ.get("CLAUDECODE") != "1" or os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    allow_push = os.environ.get("SDLC_ALLOW_PUSH", "").lower() in ("1", "true", "yes")
    if name == "pre-push" and not allow_push:
        refuse("the SDLC never pushes. The user pushes from their own terminal, or in Claude Code "
               "with `! SDLC_ALLOW_PUSH=1 git push ...`.")
    branch = sdlc.current_branch(root)
    if name == "pre-merge-commit" and (branch in sdlc.BASE_BRANCHES or branch == sdlc.default_base(root)):
        refuse(f"the SDLC never merges into '{branch}'. The user merges the PR.")
    if name == "reference-transaction":
        bases = sdlc.BASE_BRANCHES | {sdlc.default_base(root)}
        for line in data.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) != 3 or not parts[2].startswith("refs/heads/"):
                continue
            b = parts[2].removeprefix("refs/heads/")
            remotes = set((sdlc.git(root, "for-each-ref", "--format=%(objectname)",
                                    f"refs/remotes/*/{b}") or "").split())
            if b in bases and parts[1] not in remotes:
                refuse(f"refusing to move '{b}' to a commit no remote has: the SDLC never changes "
                       f"the base branch, it only fast-forwards it to a remote's {b}.")
    if name in ("pre-commit", "pre-merge-commit"):
        feat = sdlc.active_feature(root)
        reason = sdlc.check_branch(root, (feat or {}).get("base"))
        if reason:
            refuse(reason)
        if feat and (feat.get("_corrupt") or feat.get("_missing")
                     or not sdlc.approval_valid(feat, "spec") or not sdlc.approval_valid(feat, "plan")):
            staged = (sdlc.git(root, "diff", "--cached", "--name-only") or "").splitlines()
            code = [p for p in staged if Path(p).parts[:2] != sdlc.SDLC_DIR.parts]
            if code:
                refuse(f"'{feat['title']}' has no approved plan; refusing to commit code: "
                       f"{', '.join(code[:5])}. Revert or unstage them, or get the plan approved.")


def original_hooks_dir(root: Path) -> Path | None:
    """The hooks dir git would use without our session override."""
    for scope in ("--local", "--global", "--system"):
        v = sdlc.git(root, "config", scope, "--get", "core.hooksPath")
        if v:
            d = Path(os.path.expanduser(v))
            d = d if d.is_absolute() else root / d
            return None if d.resolve() == HERE else d
    gd = sdlc.git(root, "rev-parse", "--git-common-dir")
    return (root / gd / "hooks") if gd else None


def main() -> None:
    name, args = sys.argv[1], sys.argv[2:]
    data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    top = sdlc.git(Path.cwd(), "rev-parse", "--show-toplevel")
    root = Path(top) if top else Path.cwd()
    enforce(name, root, data)
    orig = original_hooks_dir(root)
    hook = orig / name if orig else None
    if hook and hook.is_file() and os.access(hook, os.X_OK):
        sys.exit(subprocess.run([str(hook), *args], input=data).returncode)


if __name__ == "__main__":
    main()
