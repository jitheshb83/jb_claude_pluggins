#!/usr/bin/env python3
"""State + branch-freshness core for the jb-sdlc-plugin.

One feature = one directory under <repo>/docs/sdlc/<date>-<slug>/ holding
spec.md, plan.md, review.md and state.json. The current phase is derived:
it is the first phase in PHASES that has no valid approval. Approvals of
spec/plan/review are bound to the sha256 of their file, so editing an
approved artifact silently re-opens that gate (and every later one).

Stdlib only — the PreToolUse hook imports this module, so it must start fast
and need no virtualenv.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PHASES = ["spec", "plan", "build", "review", "ship"]
ARTIFACT = {"spec": "spec.md", "plan": "plan.md", "review": "review.md"}
TRACKS = ("lite", "full")  # lite: ACs live in plan.md, no separate spec gate
BASE_BRANCHES = {"main", "master"}
SDLC_DIR = Path("docs") / "sdlc"


# --------------------------------------------------------------------- git


def git(cwd: Path | str, *args: str, timeout: float = 5) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def repo_root(path: Path | str) -> Path | None:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    if p.is_file():
        p = p.parent
    top = git(p, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def current_branch(root: Path) -> str:
    return git(root, "branch", "--show-current") or ""


def default_base(root: Path) -> str:
    head = git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for b in ("main", "master"):
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}") is not None:
            return b
    return "main"


def _base_ref(root: Path, base: str) -> str | None:
    for ref in (f"origin/{base}", base):
        if git(root, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


def _merged_pr(root: Path, branch: str) -> int | None:
    """PR number if the branch's PR is merged and no PR is still open. Needs gh."""
    if os.environ.get("SDLC_NO_GH") or not shutil.which("gh"):
        return None
    remote = git(root, "remote", "get-url", "origin") or ""
    if "github.com" not in remote:
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all",
             "--json", "number,state", "--limit", "20"],
            cwd=root, capture_output=True, text=True, timeout=8,
        )
        prs = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if any(p.get("state") == "OPEN" for p in prs):
        return None
    merged = [p["number"] for p in prs if p.get("state") == "MERGED"]
    return max(merged) if merged else None


def check_branch(root: Path, base: str | None = None) -> str | None:
    """None if it is safe to commit here, else a human-readable reason."""
    branch = current_branch(root)
    if not branch:
        return None  # detached HEAD (rebase, bisect) — not our business
    if branch in BASE_BRANCHES:
        return (f"you are on '{branch}'. Never commit to the base branch — "
                f"`git checkout -b feature/<name>` first.")
    base = base or default_base(root)
    if branch == base:
        return f"you are on the base branch '{base}'. Branch off it first."

    track = git(root, "for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch}")
    if track == "[gone]":
        return (f"'{branch}' tracked a remote branch that has been deleted — it was "
                f"almost certainly merged. `git checkout {base} && git pull` and branch again.")

    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ref = _base_ref(root, base)
    if upstream and ref:
        ahead = git(root, "rev-list", "--count", f"{ref}..HEAD")
        if ahead == "0" and git(root, "rev-list", "--count", f"{upstream}..HEAD") == "0":
            return (f"'{branch}' was pushed but has no commits that aren't already in "
                    f"{ref} — its work was merged. `git checkout {base} && git pull` "
                    f"and branch again.")

    pr = _merged_pr(root, branch)
    if pr:
        return (f"PR #{pr} for '{branch}' is already merged. "
                f"`git checkout {base} && git pull` and start a new branch.")
    return None


# ------------------------------------------------------------------- state


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48].rstrip("-") or "feature"


def load(state_file: Path) -> dict:
    s = json.loads(state_file.read_text())
    s["_dir"] = state_file.parent
    return s


def save(state: dict) -> None:
    d = state["_dir"]
    data = {k: v for k, v in state.items() if not k.startswith("_")}
    (d / "state.json").write_text(json.dumps(data, indent=2) + "\n")


def all_features(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / SDLC_DIR).glob("*/state.json")):
        try:
            out.append(load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def active_feature(root: Path, branch: str | None = None) -> dict | None:
    branch = current_branch(root) if branch is None else branch
    hits = [s for s in all_features(root) if s.get("branch") == branch and phase(s) != "done"]
    return max(hits, key=lambda s: s.get("created", "")) if hits else None


def approval_valid(state: dict, ph: str) -> bool:
    if ph == "spec" and state.get("track") == "lite":
        return True
    a = state.get("approvals", {}).get(ph)
    if not a:
        return False
    if ph in ARTIFACT:
        return a.get("sha256") == sha(state["_dir"] / ARTIFACT[ph])
    return True


def phase(state: dict) -> str:
    for ph in PHASES:
        if not approval_valid(state, ph):
            return ph
    return "done"


SPEC_TEMPLATE = """# Spec: {title}

<!-- Filled in by the sdlc-spec skill. Approval is bound to this file's hash:
     editing it after approval re-opens the spec gate. -->

## Problem
## Context
## Acceptance criteria
## Non-goals
## Constraints
## Open questions
"""

PLAN_LITE_TEMPLATE = """# Plan (lite): {title}

<!-- Lite track: requirements and plan in one file, one approval. Filled in
     by the sdlc-plan skill. Editing it after approval re-opens the gate. -->

## Goal
## Acceptance criteria
## Changes
## Steps
## Test plan
## Verification
"""


# --------------------------------------------------------------------- cli


def _root_or_die() -> Path:
    root = repo_root(Path.cwd())
    if not root:
        sys.exit("sdlc: not inside a git repository")
    return root


def _feature_or_die(root: Path) -> dict:
    s = active_feature(root)
    if not s:
        sys.exit(f"sdlc: no active feature for branch '{current_branch(root)}'. "
                 f"Start one with `sdlc.py new \"<title>\"`.")
    return s


def cmd_new(a) -> None:
    root = _root_or_die()
    slug = a.slug or slugify(a.title)
    base = a.base or default_base(root)
    branch = current_branch(root)
    if not branch or branch in BASE_BRANCHES or branch == base:
        branch = f"feature/{slug}"
        if git(root, "checkout", "-b", branch) is None:
            sys.exit(f"sdlc: could not create branch {branch}")
    else:
        reason = check_branch(root, base)
        if reason:
            sys.exit(f"sdlc: refusing to start on this branch: {reason}")
    if active_feature(root, branch):
        sys.exit(f"sdlc: branch '{branch}' already has an active feature — see `sdlc.py status`.")
    d = root / SDLC_DIR / f"{dt.date.today().isoformat()}-{slug}"
    d.mkdir(parents=True, exist_ok=False)
    if a.track == "full":
        (d / "spec.md").write_text(SPEC_TEMPLATE.format(title=a.title))
    else:
        (d / "plan.md").write_text(PLAN_LITE_TEMPLATE.format(title=a.title))
    state = {"title": a.title, "slug": slug, "track": a.track, "branch": branch, "base": base,
             "created": now(), "approvals": {}, "history": [{"at": now(), "event": "created"}],
             "_dir": d}
    save(state)
    print(json.dumps({"dir": str(d.relative_to(root)), "branch": branch, "base": base,
                      "track": a.track, "phase": phase(state)}))


def _status(s: dict, root: Path) -> dict:
    gates = {}
    for ph in PHASES:
        a = s.get("approvals", {}).get(ph)
        gates[ph] = "approved" if approval_valid(s, ph) else ("stale" if a else "open")
    return {"title": s["title"], "track": s.get("track", "full"),
            "dir": str(s["_dir"].relative_to(root)), "branch": s["branch"],
            "base": s["base"], "phase": phase(s), "gates": gates,
            "evidence": s.get("approvals", {}).get("build", {}).get("evidence")}


def cmd_status(a) -> None:
    root = _root_or_die()
    s = active_feature(root)
    if not s:
        print(json.dumps({"active": None, "branch": current_branch(root),
                          "features": [_status(f, root) for f in all_features(root)][-5:]}, indent=2))
        return
    print(json.dumps(_status(s, root), indent=2))


def cmd_approve(a) -> None:
    root = _root_or_die()
    s = _feature_or_die(root)
    ph = a.phase
    if ph == "spec" and s.get("track") == "lite":
        sys.exit("sdlc: lite track has no spec gate — approve the plan "
                 "(or `sdlc.py track full` to add a spec).")
    cur = phase(s)
    if cur != "done" and PHASES.index(ph) > PHASES.index(cur):
        sys.exit(f"sdlc: cannot approve '{ph}' — '{cur}' is not approved yet.")
    entry: dict = {"at": now(), "note": a.note or ""}
    if ph in ARTIFACT:
        f = s["_dir"] / ARTIFACT[ph]
        if not f.exists() or not f.read_text().strip():
            sys.exit(f"sdlc: {f.relative_to(root)} is missing or empty.")
        if ph == "review" and not re.search(r"(?im)^[#*\s]*verdict\W*pass\b", f.read_text()):
            sys.exit("sdlc: review.md has no 'Verdict: PASS' line — fix findings first.")
        entry["sha256"] = sha(f)
    if ph == "build":
        if not a.evidence:
            sys.exit("sdlc: approving build needs --evidence (the test command and its result).")
        entry["evidence"] = a.evidence
    if ph == "ship":
        if not a.pr:
            sys.exit("sdlc: approving ship needs --pr <url>.")
        entry["pr"] = a.pr
    s.setdefault("approvals", {})[ph] = entry
    s.setdefault("history", []).append({"at": now(), "event": f"approved {ph}"})
    save(s)
    print(json.dumps(_status(s, root), indent=2))


def cmd_reopen(a) -> None:
    root = _root_or_die()
    s = _feature_or_die(root)
    for ph in PHASES[PHASES.index(a.phase):]:
        s.get("approvals", {}).pop(ph, None)
    s.setdefault("history", []).append({"at": now(), "event": f"reopened {a.phase}: {a.note or ''}"})
    save(s)
    print(json.dumps(_status(s, root), indent=2))


def cmd_track(a) -> None:
    root = _root_or_die()
    s = _feature_or_die(root)
    s["track"] = a.track
    if a.track == "full":
        spec = s["_dir"] / "spec.md"
        if not spec.exists():
            spec.write_text(SPEC_TEMPLATE.format(title=s["title"]))
        for ph in PHASES:  # escalating re-opens everything: the spec now leads
            s.get("approvals", {}).pop(ph, None)
    s.setdefault("history", []).append({"at": now(), "event": f"track -> {a.track}"})
    save(s)
    print(json.dumps(_status(s, root), indent=2))


def cmd_check_branch(a) -> None:
    root = _root_or_die()
    s = active_feature(root)
    reason = check_branch(root, a.base or (s or {}).get("base"))
    if reason:
        sys.exit(f"STALE: {reason}")
    print(f"OK: '{current_branch(root)}' is a fresh feature branch")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="sdlc.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="start a feature (creates branch + docs/sdlc dir)")
    n.add_argument("title")
    n.add_argument("--slug")
    n.add_argument("--base")
    n.add_argument("--track", choices=TRACKS, default="lite")
    tr = sub.add_parser("track", help="switch the active feature between lite and full")
    tr.add_argument("track", choices=TRACKS)
    sub.add_parser("status", help="show the active feature for this branch")
    ap = sub.add_parser("approve", help="record a gate approval")
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--note", help="who approved / what was said")
    ap.add_argument("--evidence", help="build: test command + result")
    ap.add_argument("--pr", help="ship: PR URL")
    ro = sub.add_parser("reopen", help="clear a gate and every later one")
    ro.add_argument("phase", choices=PHASES)
    ro.add_argument("--note")
    cb = sub.add_parser("check-branch", help="exit non-zero if this branch is stale/base")
    cb.add_argument("--base")
    a = p.parse_args(argv)
    {"new": cmd_new, "status": cmd_status, "approve": cmd_approve,
     "reopen": cmd_reopen, "track": cmd_track, "check-branch": cmd_check_branch}[a.cmd](a)


if __name__ == "__main__":
    main()
