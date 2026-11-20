#!/usr/bin/env python3
"""State + branch-freshness core for the jb-sdlc-plugin.

One feature = one directory under <repo>/docs/sdlc/<date>-<slug>/ holding
spec.md, plan.md, review.md, pr.md and state.json. The current phase is derived:
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


def _branch_created(root: Path, branch: str) -> float | None:
    """Unix time of the branch's oldest reflog entry (its creation)."""
    log = git(root, "reflog", "show", "--date=unix", "--format=%gd", f"refs/heads/{branch}") or ""
    stamps = re.findall(r"@\{(\d+)\}", log)
    return float(stamps[-1]) if stamps else None


def pick_merged(prs: list[dict], created: float | None) -> int | None:
    """The merged PR that belongs to this incarnation of the branch, if any.
    A PR merged before the branch was (re)created is an older branch's PR."""
    if any(p.get("state") == "OPEN" for p in prs):
        return None
    hits = []
    for p in prs:
        if p.get("state") != "MERGED":
            continue
        at = p.get("mergedAt")
        try:
            ts = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() if at else None
        except ValueError:
            ts = None
        if created is None or ts is None or ts > created:
            hits.append(p["number"])
    return max(hits) if hits else None


def _merged_pr(root: Path, branch: str) -> int | None:
    """PR number if this branch's PR is merged and no PR is still open. Needs gh."""
    if os.environ.get("SDLC_NO_GH") or not shutil.which("gh"):
        return None
    remote = git(root, "remote", "get-url", "origin") or ""
    if "github.com" not in remote:
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all",
             "--json", "number,state,mergedAt", "--limit", "20"],
            cwd=root, capture_output=True, text=True, timeout=4,
        )
        prs = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return pick_merged(prs, _branch_created(root, branch))


def _had_own_commits(root: Path, branch: str) -> bool:
    """True if anything was ever committed on this branch (reflog evidence).
    A freshly created branch, pushed or not, has only a 'branch: Created' entry."""
    log = git(root, "reflog", "show", "--format=%gs", f"refs/heads/{branch}") or ""
    return any(ln.startswith(("commit", "merge", "cherry-pick")) for ln in log.splitlines())


def refresh_remote(root: Path, max_age: float = 60) -> None:
    """`git fetch --prune origin`, at most once a minute, never fatal. Without it a
    PR merged on GitHub mid-session is invisible when gh isn't available."""
    if os.environ.get("SDLC_NO_FETCH") or not git(root, "remote", "get-url", "origin"):
        return
    gd = git(root, "rev-parse", "--git-common-dir")
    stamp = (root / gd / "sdlc-fetch-stamp") if gd else None
    try:
        if stamp and stamp.exists() and dt.datetime.now().timestamp() - stamp.stat().st_mtime < max_age:
            return
        if stamp:
            stamp.touch()  # stamped before fetching, so a hanging remote is retried only once a minute
    except OSError:
        pass
    git(root, "fetch", "--prune", "-q", "origin", timeout=4)


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
    refresh_remote(root)

    track = git(root, "for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch}")
    if track == "[gone]":
        return (f"'{branch}' tracked a remote branch that has been deleted — it was "
                f"almost certainly merged. `git checkout {base} && git pull` and branch again.")

    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ref = _base_ref(root, base)
    if upstream and ref and _had_own_commits(root, branch):
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


# ---------------------------------------------------------------- diagrams

MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
                 "erDiagram", "classDiagram", "gantt", "journey", "mindmap", "timeline", "pie",
                 "gitGraph", "quadrantChart", "block-beta", "C4Context", "C4Container",
                 "requirementDiagram", "xychart-beta", "sankey-beta", "packet-beta",
                 "architecture-beta", "kanban", "C4Component", "C4Dynamic", "C4Deployment")
_FENCE = re.compile(r"^```mermaid[^\n]*\n(.*?)^```", re.M | re.S)
_SHAPES = [("([", "])"), ("[(", ")]"), ("((", "))"), ("{{", "}}"), ("[[", "]]"),
           ("[/", "/]"), ("[\\", "\\]"), ("[", "]"), ("(", ")"), ("{", "}"), (">", "]")]
_NODE = re.compile(r"(?:^|[\s;&>-])([A-Za-z_][\w-]*)(?=\(\[|\[\(|\(\(|\{\{|\[\[|\[/|\[\\\\|\[|\(|\{)")
_BLOCK_OPEN = re.compile(r"^\s*(alt|opt|loop|par|critical|break|rect|box)\b")


def mermaid_blocks(text: str) -> list[str]:
    return _FENCE.findall(text)


def _labels(line: str):
    """Yield (node_id, label) for node shapes declared on a flowchart line."""
    for m in _NODE.finditer(line):
        rest = line[m.end():]
        for op, cl in _SHAPES:
            if rest.startswith(op):
                body = rest[len(op):]
                if body.startswith('"'):
                    close = body.find('"', 1)
                    yield m.group(1), body[: close + 1] if close > 0 else body
                else:
                    yield m.group(1), body.split(cl, 1)[0]
                break


def lint_mermaid(block: str) -> list[str]:
    errs: list[str] = []
    raw = block.splitlines()
    if raw and raw[0].strip() == "---":  # YAML front matter (title/config)
        close = next((i for i, ln in enumerate(raw[1:], 1) if ln.strip() == "---"), None)
        raw = raw[close + 1:] if close else raw
    lines = [ln for ln in raw if ln.strip() and not ln.strip().startswith("%%")]
    if not lines:
        return ["empty diagram"]
    head = lines[0].split()
    kind = head[0].rstrip(";")
    if kind not in MERMAID_TYPES:
        return [f"unknown diagram type '{kind}'"]
    if kind in ("flowchart", "graph") and len(head) > 1 and head[1].rstrip(";") not in ("TB", "TD", "BT", "RL", "LR"):
        errs.append(f"bad direction '{head[1]}'")
    body = lines[1:]
    ends = sum(1 for ln in body if ln.strip() == "end")
    if kind in ("flowchart", "graph"):
        subs = sum(1 for ln in body if ln.strip().startswith("subgraph"))
        if subs != ends:
            errs.append(f"{subs} subgraph vs {ends} end")
        defined = {n for ln in body if ln.strip().startswith("classDef") and len(ln.split()) > 1
                   for n in ln.split()[1].split(",")}
        used = set(re.findall(r":::([\w-]+)", block))
        for ln in body:
            if ln.strip().startswith("class ") and len(ln.split()) > 2:
                used.update(n for n in ln.split()[-1].rstrip(";").split(",") if n)
        for c in sorted(used - defined):
            errs.append(f"class '{c}' used but no classDef")
        for ln in body:
            if ln.strip().startswith(("classDef", "class ", "style ", "linkStyle", "click ")):
                continue
            for nid, label in _labels(ln):
                if nid == "end":
                    errs.append("node id 'end' breaks flowcharts; rename it")
                if not label.startswith('"') and re.search(r'[()\[\]{}"|]', label):
                    errs.append(f"quote the label of '{nid}': {label.strip()[:30]}")
    elif kind == "sequenceDiagram":
        opens = sum(1 for ln in body if _BLOCK_OPEN.match(ln))
        if opens != ends:
            errs.append(f"{opens} alt/opt/loop/par blocks vs {ends} end")
    return errs


def _mmdc(block: str) -> str | None:
    """Full render check with mermaid-cli when it is installed locally."""
    if not shutil.which("mmdc"):
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        src = Path(t) / "d.mmd"
        src.write_text(block)
        try:
            r = subprocess.run(["mmdc", "-q", "-i", str(src), "-o", str(Path(t) / "d.svg")],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["render failed"])[-1]


def check_diagrams(path: Path, require: bool = False) -> list[str]:
    text = path.read_text() if path.exists() else ""
    blocks = mermaid_blocks(text)
    errs = [] if blocks or not require else [f"{path.name}: needs at least one mermaid diagram "
                                             f"(see references/diagrams.md)"]
    for i, b in enumerate(blocks, 1):
        errs += [f"{path.name} diagram {i}: {e}" for e in lint_mermaid(b)]
        rendered = _mmdc(b) if not errs else None
        if rendered:
            errs.append(f"{path.name} diagram {i}: mmdc: {rendered}")
    return errs


def diagram_required(state: dict, ph: str) -> bool:
    if ph == "spec":
        return True  # spec gate exists only on the full track
    if ph == "plan":
        plan = state["_dir"] / "plan.md"
        lanes = plan.exists() and re.search(r"(?m)^Lane [A-C]\b", plan.read_text())
        return state.get("track") == "full" or bool(lanes)
    return False


# ----------------------------------------------------------------- content


def sections(text: str) -> dict[str, list[str]]:
    """'## heading' (lowercased, comments stripped) -> its meaningful lines."""
    out: dict[str, list[str]] = {}
    cur, fence = None, False
    for ln in text.splitlines():
        st = ln.strip()
        if st.startswith(("```", "~~~")):
            fence = not fence
            if cur is not None and fence:
                out[cur].append("[diagram]")
            continue
        if not fence and ln.startswith("## "):
            cur = re.sub(r"<!--.*?-->", "", ln[3:]).strip().lower()
            out.setdefault(cur, [])
            continue
        if cur is None or fence or not st or st.startswith("<!--") or re.fullmatch(r"<[^>]*>", st):
            continue
        out[cur].append(st)
    return out


def _section(secs: dict, name: str) -> list[str] | None:
    return next((v for k, v in secs.items() if k.startswith(name)), None)


CODE_EXT = ("py|pyi|kt|kts|java|ts|tsx|js|jsx|mjs|go|rs|swift|m|c|h|cc|cpp|hpp|cs|rb|php|sh|zsh|"
            "md|json|yaml|yml|toml|ini|cfg|xml|plist|html|css|sql|gradle|txt|lock|spec|ui|qml")
EVIDENCE_REF = re.compile(
    r"(?:[\w.-]+/)+[\w.-]+(?::\d+)?"               # a path with a directory
    r"|\b[\w-]+\.(?:" + CODE_EXT + r")\b(?::\d+)?"  # file.ext[:line]
    r"|`[^`]+`|nothing existing", re.I)
NONE_LINE = re.compile(r"^(?:[-*]\s*)?(?:none|n/a)\.?$", re.I)


def check_content(state: dict, ph: str) -> list[str]:
    """Approval blockers in spec.md / plan.md: unfilled sections, no evidence,
    unresolved questions. Answers from the discussion belong under ## Decisions."""
    f = state["_dir"] / ARTIFACT[ph]
    secs = sections(f.read_text())
    need = {"spec": ["problem", "acceptance criteria", "evidence"],
            "plan": ["steps", "test plan", "evidence"]}[ph]
    if ph == "plan" and state.get("track") == "lite":
        need = ["goal", "acceptance criteria"] + need
    errs = [f"{f.name}: '## {n}' is missing or still empty" for n in need if not _section(secs, n)]
    ev = _section(secs, "evidence")
    if ev and not any(EVIDENCE_REF.search(ln) for ln in ev):
        errs.append(f"{f.name}: '## Evidence' must cite what was actually read or run "
                    f"(path:line or a `command`), or say 'nothing existing' for new code")
    oq = [ln for ln in (_section(secs, "open questions") or []) if not NONE_LINE.match(ln)]
    if oq:
        errs.append(f"{f.name}: {len(oq)} open question(s): clarify with the user, record the answers "
                    f"under '## Decisions', then leave '## Open questions' as 'None'")
    return errs


# ---------------------------------------------------------------- baseline


def _baseline_file(root: Path, state: dict) -> Path | None:
    gd = git(root, "rev-parse", "--git-common-dir")
    return (root / gd / f"sdlc-baseline-{state['_dir'].name}.json") if gd else None


def dirty_code(root: Path) -> dict[str, str]:
    """Changed or untracked files outside docs/sdlc -> content hash ('' if deleted)."""
    out = {}
    try:  # not via git(): it strips, which would eat the first entry's status column
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall", "-z"],
                                capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        status = ""
    for entry in status.split("\0"):
        if len(entry) < 4:
            continue
        rel = entry[3:]
        if Path(rel).parts[:2] == SDLC_DIR.parts:
            continue
        f = root / rel
        out[rel] = (git(root, "hash-object", "--", rel) or "") if f.is_file() else ""
    return out


def save_baseline(root: Path, state: dict) -> None:
    bf = _baseline_file(root, state)
    if bf:
        bf.write_text(json.dumps(dirty_code(root)))


def drift(root: Path, state: dict) -> list[str]:
    """Code files changed since the feature started (shell writes the hook can't see)."""
    bf = _baseline_file(root, state)
    try:
        base = json.loads(bf.read_text()) if bf and bf.exists() else {}
    except ValueError:
        base = {}
    return sorted(p for p, h in dirty_code(root).items() if base.get(p) != h)


# ------------------------------------------------------------------- state


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48].rstrip("-") or "feature"


def _valid_state(s) -> bool:
    return (isinstance(s, dict) and isinstance(s.get("title"), str) and isinstance(s.get("branch"), str)
            and isinstance(s.get("approvals", {}), dict))


def load(state_file: Path) -> dict:
    """Parse a state.json. Anything unreadable comes back as a *corrupt* state,
    which blocks code changes instead of silently switching the gate off."""
    try:
        s = json.loads(state_file.read_text())
    except (OSError, ValueError):
        s = None
    if not _valid_state(s):
        return {"title": f"unreadable {state_file.parent.name}/state.json", "branch": None,
                "approvals": {}, "_dir": state_file.parent, "_corrupt": True}
    s["_dir"] = state_file.parent
    return s


def save(state: dict) -> None:
    d = state["_dir"]
    data = {k: v for k, v in state.items() if not k.startswith("_")}
    (d / "state.json").write_text(json.dumps(data, indent=2) + "\n")


def all_features(root: Path) -> list[dict]:
    return [load(f) for f in sorted((root / SDLC_DIR).glob("*/state.json"))]


def _deleted_feature(root: Path, branch: str) -> dict | None:
    """A feature committed on this branch whose folder is gone from the worktree."""
    listed = git(root, "ls-tree", "-r", "--name-only", "HEAD", "--", str(SDLC_DIR)) or ""
    for rel in listed.splitlines():
        if not rel.endswith("/state.json") or (root / rel).exists():
            continue
        try:
            s = json.loads(git(root, "show", f"HEAD:{rel}") or "")
        except ValueError:
            continue
        if _valid_state(s) and s["branch"] == branch:
            s["_dir"], s["_missing"] = root / Path(rel).parent, True
            if phase(s) != "done":
                return s
    return None


def active_feature(root: Path, branch: str | None = None) -> dict | None:
    branch = current_branch(root) if branch is None else branch
    feats = all_features(root)
    corrupt = [s for s in feats if s.get("_corrupt")]
    if corrupt:
        return corrupt[0]  # fail closed: an unreadable state blocks until the user fixes it
    hits = [s for s in feats if s.get("branch") == branch and phase(s) != "done"]
    if hits:
        return max(hits, key=lambda s: s.get("created", ""))
    return _deleted_feature(root, branch) if branch else None


def approval_valid(state: dict, ph: str) -> bool:
    if state.get("_corrupt") or state.get("_missing"):
        return False
    if ph == "spec" and state.get("track") == "lite":
        return True
    a = state.get("approvals", {}).get(ph)
    if not a:
        return False
    if ph in ARTIFACT:
        return a.get("sha256") == sha(state["_dir"] / ARTIFACT[ph])
    return True


def phase(state: dict) -> str:
    if state.get("_corrupt"):
        return "corrupt"
    for ph in PHASES:
        if not approval_valid(state, ph):
            return ph
    return "done"


SPEC_TEMPLATE = """# Spec: {title}

<!-- Filled in by the sdlc-spec skill. Approval is bound to this file's hash:
     editing it after approval re-opens the spec gate. -->

## Problem
## User flow
<mermaid flowchart: see references/diagrams.md>
## Context
## Evidence
<one bullet per claim about existing code: path:line or `command` + what it showed>
## Acceptance criteria
## Non-goals
## Constraints
## Decisions
<question -> the user's answer, from the discussion>
## Open questions
None
"""

PLAN_LITE_TEMPLATE = """# Plan (lite): {title}

<!-- Lite track: requirements and plan in one file, one approval. Filled in
     by the sdlc-plan skill. Editing it after approval re-opens the gate. -->

## Goal
## Evidence
<one bullet per claim about existing code: path:line or `command` + what it showed>
## Acceptance criteria
## Changes
## Steps
## Test plan
## Verification
## Decisions
<question -> the user's answer, from the discussion>
## Open questions
None
"""


# --------------------------------------------------------------------- cli


def _root_or_die() -> Path:
    root = repo_root(Path.cwd())
    if not root:
        sys.exit("sdlc: not inside a git repository")
    return root


def _feature_or_die(root: Path) -> dict:
    s = active_feature(root)
    if s and s.get("_corrupt"):
        sys.exit(f"sdlc: {s['_dir'].relative_to(root)}/state.json is unreadable (merge conflict?). "
                 f"Ask the user to fix it by hand; code changes stay blocked until then.")
    if s and s.get("_missing"):
        sys.exit(f"sdlc: the feature folder {s['_dir'].relative_to(root)} was deleted from the working "
                 f"tree. Ask the user to restore it: git checkout HEAD -- {s['_dir'].relative_to(root)}")
    if not s:
        sys.exit(f"sdlc: no active feature for branch '{current_branch(root)}'. "
                 f"Start one with `sdlc.py new \"<title>\"`.")
    return s


def cmd_new(a) -> None:
    root = _root_or_die()
    slug = a.slug or slugify(a.title)
    base = a.base or default_base(root)
    branch = current_branch(root)
    on_base = not branch or branch in BASE_BRANCHES or branch == base
    if on_base:
        branch = f"feature/{slug}"
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") is not None:
            sys.exit(f"sdlc: branch {branch} already exists — pick another --slug.")
    else:
        reason = check_branch(root, base)
        if reason:
            sys.exit(f"sdlc: refusing to start on this branch: {reason}")
        if active_feature(root, branch):
            sys.exit(f"sdlc: branch '{branch}' already has an active feature — see `sdlc.py status`.")
    stem = f"{dt.date.today().isoformat()}-{slug}"
    d, n = root / SDLC_DIR / stem, 2
    while d.exists():
        d, n = root / SDLC_DIR / f"{stem}-{n}", n + 1
    if on_base:
        has_origin = bool(git(root, "remote", "get-url", "origin"))
        start: list[str] = []
        if a.base and a.base != current_branch(root):
            if has_origin and not a.no_pull:
                git(root, "fetch", "-q", "origin", a.base, timeout=60)
            ref = _base_ref(root, a.base)
            if not ref:
                sys.exit(f"sdlc: base '{a.base}' not found locally or on origin.")
            start = ["--no-track", ref]
        elif has_origin and not a.no_pull:
            pulled = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "-q"],
                                    capture_output=True, text=True, timeout=60)
            if pulled.returncode != 0:
                sys.exit(f"sdlc: `git pull --ff-only` on {current_branch(root) or base} failed — "
                         f"sync the base first (or --no-pull when offline):\n"
                         f"{pulled.stderr.strip()}")
        if git(root, "checkout", "-q", "-b", branch, *start) is None:
            sys.exit(f"sdlc: could not create branch {branch}")
    d.mkdir(parents=True)
    if a.track == "full":
        (d / "spec.md").write_text(SPEC_TEMPLATE.format(title=a.title))
    else:
        (d / "plan.md").write_text(PLAN_LITE_TEMPLATE.format(title=a.title))
    state = {"title": a.title, "slug": slug, "track": a.track, "branch": branch, "base": base,
             "created": now(), "approvals": {}, "history": [{"at": now(), "event": "created"}],
             "_dir": d}
    save(state)
    save_baseline(root, state)
    print(json.dumps({"dir": str(d.relative_to(root)), "branch": branch, "base": base,
                      "track": a.track, "phase": phase(state)}))


def _status(s: dict, root: Path) -> dict:
    gates = {}
    for ph in PHASES:
        a = s.get("approvals", {}).get(ph)
        gates[ph] = "approved" if approval_valid(s, ph) else ("stale" if a else "open")
    return {"title": s["title"], "track": s.get("track", "full"),
            "dir": str(s["_dir"].relative_to(root)), "branch": s.get("branch"),
            "base": s.get("base"), "phase": phase(s), "gates": gates,
            "evidence": s.get("approvals", {}).get("build", {}).get("evidence")}


def cmd_status(a) -> None:
    root = _root_or_die()
    s = active_feature(root)
    if s and (s.get("_corrupt") or s.get("_missing")):
        _feature_or_die(root)
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
        if ph in ("spec", "plan"):
            errs = check_diagrams(f, require=diagram_required(s, ph)) + check_content(s, ph)
            if not (a.note or "").strip():
                errs.append("--note is required: quote the user's approval of this exact file")
            if errs:
                sys.exit("sdlc: not ready to approve:\n  " + "\n  ".join(errs))
        if ph == "review" and not re.search(r"(?im)^[#*\s]*verdict\W*pass\b", f.read_text()):
            sys.exit("sdlc: review.md has no 'Verdict: PASS' line — fix findings first.")
        entry["sha256"] = sha(f)
    if ph == "build":
        if not a.evidence:
            sys.exit("sdlc: approving build needs --evidence (the test command and its result).")
        entry["evidence"] = a.evidence
    if ph == "ship":
        pr_md = s["_dir"] / "pr.md"
        if not pr_md.exists() or not pr_md.read_text().strip():
            sys.exit(f"sdlc: write {pr_md.relative_to(root)} (the PR description) first.")
        entry["head"] = git(root, "rev-parse", "--short", "HEAD")
        if a.pr:
            entry["pr"] = a.pr
    prev = s.get("approvals", {}).get(ph)
    if ph in ARTIFACT and (not prev or prev.get("sha256") != entry.get("sha256")):
        for later in PHASES[PHASES.index(ph) + 1:]:
            s.get("approvals", {}).pop(later, None)  # built/reviewed against the old text
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
    if s.get("track") == a.track:
        sys.exit(f"sdlc: already on the {a.track} track.")
    if a.track == "lite":
        if not (a.note or "").strip():
            sys.exit("sdlc: dropping to lite removes the spec gate — needs --note with the user's words.")
        for ph in PHASES[PHASES.index("plan"):]:
            s.get("approvals", {}).pop(ph, None)  # the lite plan must carry the ACs and be re-approved
    s["track"] = a.track
    if a.track == "full":
        spec = s["_dir"] / "spec.md"
        if not spec.exists():
            spec.write_text(SPEC_TEMPLATE.format(title=s["title"]))
        for ph in PHASES:  # escalating re-opens everything: the spec now leads
            s.get("approvals", {}).pop(ph, None)
    s.setdefault("history", []).append({"at": now(), "event": f"track -> {a.track}: {a.note or ''}"})
    save(s)
    print(json.dumps(_status(s, root), indent=2))


def cmd_check_diagrams(a) -> None:
    if a.files:
        errs = [e for f in a.files for e in check_diagrams(Path(f))]
    else:
        root = _root_or_die()
        s = _feature_or_die(root)
        errs = [e for ph in ("spec", "plan") if (s["_dir"] / ARTIFACT[ph]).exists()
                for e in check_diagrams(s["_dir"] / ARTIFACT[ph], require=diagram_required(s, ph))]
    if errs:
        sys.exit("DIAGRAMS:\n  " + "\n  ".join(errs))
    print("OK: diagrams lint clean" + ("" if shutil.which("mmdc") else " (mmdc not installed: syntax lint only)"))


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
    n.add_argument("--no-pull", action="store_true", help="skip `git pull --ff-only` of the base")
    tr = sub.add_parser("track", help="switch the active feature between lite and full")
    tr.add_argument("track", choices=TRACKS)
    tr.add_argument("--note", help="the user's words (required to drop to lite)")
    sub.add_parser("status", help="show the active feature for this branch")
    ap = sub.add_parser("approve", help="record a gate approval")
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--note", help="who approved / what was said")
    ap.add_argument("--evidence", help="build: test command + result")
    ap.add_argument("--pr", help="ship: PR URL, if the user already opened one")
    ro = sub.add_parser("reopen", help="clear a gate and every later one")
    ro.add_argument("phase", choices=PHASES)
    ro.add_argument("--note")
    cd = sub.add_parser("check-diagrams", help="lint mermaid diagrams (active spec/plan, or given files)")
    cd.add_argument("files", nargs="*")
    cb = sub.add_parser("check-branch", help="exit non-zero if this branch is stale/base")
    cb.add_argument("--base")
    a = p.parse_args(argv)
    {"new": cmd_new, "status": cmd_status, "approve": cmd_approve,
     "reopen": cmd_reopen, "track": cmd_track, "check-diagrams": cmd_check_diagrams, "check-branch": cmd_check_branch}[a.cmd](a)


if __name__ == "__main__":
    main()
