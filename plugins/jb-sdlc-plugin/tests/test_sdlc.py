"""End-to-end tests for sdlc.py and the PreToolUse gate, against throwaway git repos."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
CLI = PLUGIN / "scripts" / "sdlc.py"
GATE = PLUGIN / "hooks" / "gate.py"
ENV = {**os.environ, "SDLC_NO_GH": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
for _k in [k for k in ENV if k.startswith(("SDLC_", "GIT_CONFIG_")) or k in ("CLAUDECODE", "GIT_CONFIG_COUNT")]:
    ENV.pop(_k)  # tests must not inherit a plugin-enabled session's hooks
ENV["SDLC_NO_GH"] = "1"
DIAG = '\n```mermaid\nflowchart LR\n    A["parse(x)"]:::new --> B\n    classDef new fill:#DCFCE7\n```\n'
GOOD_SPEC = ("# Spec\n## Problem\n- totals double count\n## User flow\n" + DIAG +
             "## Evidence\n- app.py:1 sets x = 1\n## Acceptance criteria\n- AC1: given x, then y\n"
             "## Decisions\n- currency? -> NOK only (user)\n## Open questions\nNone\n")
GOOD_PLAN = ("# Plan\n## Goal\n- fix totals\n## Change map\n" + DIAG +
             "## Evidence\n- app.py:1 sets x = 1\n## Acceptance criteria\n- AC1: given x, then y\n"
             "## Steps\n1. do it\n## Test plan\n| AC1 | tests/test_app.py::test_y |\n"
             "## Open questions\nNone\n")
GOOD_PLAN_NODIAG = GOOD_PLAN.replace(DIAG, "")



def sh(cwd, *args, check=True):
    return subprocess.run(args, cwd=cwd, env=ENV, capture_output=True, text=True, check=check)


def cli(cwd, *args, check=True):
    return sh(cwd, sys.executable, str(CLI), *args, check=check)


def gate(cwd, tool, tool_input, env=None):
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
    return subprocess.run([sys.executable, str(GATE)], input=json.dumps(event),
                          env=env or ENV, capture_output=True, text=True)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.remote = base / "remote.git"
        self.repo = base / "repo"
        sh(base, "git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        sh(base, "git", "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "app.py").write_text("x = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "remote", "add", "origin", str(self.remote))
        sh(self.repo, "git", "push", "-qu", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        return json.loads(cli(self.repo, "status").stdout)

    def feature_dir(self):
        return self.repo / self.status()["dir"]


class TestLifecycle(Repo):
    def test_full_happy_path_and_hash_bound_approvals(self):
        out = json.loads(cli(self.repo, "new", "Card import", "--track", "full").stdout)
        self.assertEqual(out["branch"], "feature/card-import")
        self.assertEqual(sh(self.repo, "git", "branch", "--show-current").stdout.strip(),
                         "feature/card-import")
        self.assertEqual(self.status()["phase"], "spec")

        # cannot skip ahead
        r = cli(self.repo, "approve", "plan", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not approved yet", r.stderr)

        d = self.feature_dir()
        (d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "user: yes")
        r = cli(self.repo, "approve", "plan", check=False)
        self.assertIn("missing or empty", r.stderr)
        (d / "plan.md").write_text(GOOD_PLAN)
        cli(self.repo, "approve", "plan", "--note", "user: go")
        self.assertEqual(self.status()["phase"], "build")

        # editing an approved plan re-opens it
        (d / "plan.md").write_text(GOOD_PLAN.replace("do it", "do it v2"))
        st = self.status()
        self.assertEqual(st["phase"], "plan")
        self.assertEqual(st["gates"]["plan"], "stale")
        cli(self.repo, "approve", "plan", "--note", "re-approved")

        self.assertIn("--evidence", cli(self.repo, "approve", "build", check=False).stderr)
        cli(self.repo, "approve", "build", "--evidence", "pytest: 3 passed")

        (d / "review.md").write_text("# Review\n\n## Verdict: FAIL\n")
        self.assertIn("Verdict: PASS", cli(self.repo, "approve", "review", check=False).stderr)
        (d / "review.md").write_text("# Review\n\n## Verdict: PASS\n")
        cli(self.repo, "approve", "review")

        self.assertIn("pr.md", cli(self.repo, "approve", "ship", check=False).stderr)
        (d / "pr.md").write_text("Fix totals.\n")
        st = json.loads(cli(self.repo, "approve", "ship").stdout)  # no push, no PR URL needed
        self.assertEqual(st["gates"]["ship"], "approved")
        self.assertIsNone(self.status()["active"])  # done features aren't active

    def test_reopen_clears_later_gates(self):
        cli(self.repo, "new", "Thing", "--track", "full")
        d = self.feature_dir()
        (d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "user: yes")
        (d / "plan.md").write_text(GOOD_PLAN)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        cli(self.repo, "approve", "build", "--evidence", "ok")
        cli(self.repo, "reopen", "plan", "--note", "scope change")
        g = self.status()["gates"]
        self.assertEqual((g["spec"], g["plan"], g["build"]), ("approved", "open", "open"))


class TestLiteTrack(Repo):
    def test_lite_is_default_and_skips_spec(self):
        out = json.loads(cli(self.repo, "new", "Small tweak").stdout)
        self.assertEqual((out["track"], out["phase"]), ("lite", "plan"))
        d = self.feature_dir()
        self.assertFalse((d / "spec.md").exists())
        self.assertIn("Acceptance criteria", (d / "plan.md").read_text())
        self.assertIn("no spec gate", cli(self.repo, "approve", "spec", check=False).stderr)
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)
        self.assertIn("still empty", cli(self.repo, "approve", "plan", "--note", "ok", check=False).stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: ok")
        self.assertEqual(self.status()["phase"], "build")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)

    def test_escalating_to_full_adds_spec_and_reopens(self):
        cli(self.repo, "new", "Grows")
        (self.feature_dir() / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        st = json.loads(cli(self.repo, "track", "full").stdout)
        self.assertEqual((st["track"], st["phase"]), ("full", "spec"))
        self.assertTrue((self.feature_dir() / "spec.md").exists())
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)


ROUTE = PLUGIN / "hooks" / "route.py"


def route(cwd, prompt, env=None):
    event = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(cwd)}
    return subprocess.run([sys.executable, str(ROUTE)], input=json.dumps(event),
                          env=env or ENV, capture_output=True, text=True)


class TestRouter(Repo):
    def test_dev_prompts_get_a_pointer(self):
        for p in ["Implement CSV export for the loan schedule",
                  "can you fix the bug where totals double count",
                  "take this task: add a dark mode setting",
                  "Build a script that renames statement PDFs",
                  "please work on issue 14"]:
            r = route(self.repo, p)
            self.assertEqual(r.returncode, 0)
            self.assertIn("sdlc skill", r.stdout, p)

    def test_non_dev_prompts_add_nothing(self):
        for p in ["what does amortization mean?", "how does the gate hook work?",
                  "summarise my calendar for tomorrow", "/sdlc status", "thanks!"]:
            r = route(self.repo, p)
            self.assertEqual((r.returncode, r.stdout), (0, ""), p)

    def test_active_feature_is_announced_on_any_prompt(self):
        cli(self.repo, "new", "Card import")
        r = route(self.repo, "ok, continue")
        self.assertIn('"Card import"', r.stdout)
        self.assertIn("phase: plan", r.stdout)

    def test_off_switch_and_garbage(self):
        self.assertEqual(route(self.repo, "implement X", env={**ENV, "SDLC_AUTOROUTE": "off"}).stdout, "")
        r = subprocess.run([sys.executable, str(ROUTE)], input="{bad", env=ENV,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class TestWorktrees(Repo):
    def approved_feature(self):
        cli(self.repo, "new", "Lanes")
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN.replace("1. do it", "Lane A (files: a.py)\n1. do it\nLane B (files: b.py)\n2. more"))
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs: plan")

    def test_parallel_lanes_build_commit_and_merge(self):
        self.approved_feature()
        lanes = {}
        for lane, fname in (("a", "a.py"), ("b", "b.py")):
            wt = Path(self.tmp.name) / f"wt-{lane}"
            sh(self.repo, "git", "worktree", "add", "-q", "-b", f"lane-{lane}", str(wt))
            # gates inside a lane worktree: edits and commits allowed
            self.assertEqual(gate(wt, "Write", {"file_path": str(wt / fname)}).returncode, 0)
            (wt / fname).write_text(f"{lane} = 1\n")
            sh(wt, "git", "add", fname)
            self.assertEqual(gate(wt, "Bash", {"command": f"git commit -m lane-{lane}"}).returncode, 0)
            sh(wt, "git", "commit", "-qm", f"lane {lane}")
            lanes[lane] = wt
        for lane in lanes:  # merge in lane order on the feature branch
            sh(self.repo, "git", "merge", "--no-ff", "--no-edit", "-q", f"lane-{lane}")
        self.assertTrue((self.repo / "a.py").exists() and (self.repo / "b.py").exists())
        self.assertEqual(self.status()["phase"], "build")  # approvals survive the merges
        for lane, wt in lanes.items():
            sh(self.repo, "git", "worktree", "remove", str(wt))
            sh(self.repo, "git", "branch", "-d", f"lane-{lane}")

    def test_multi_task_worktree_gets_its_own_gated_feature(self):
        wt = Path(self.tmp.name) / "repo-task2"
        sh(self.repo, "git", "worktree", "add", "-q", "-b", "feature/task2", str(wt), "main")
        out = json.loads(cli(wt, "new", "Task two").stdout)
        self.assertEqual(out["branch"], "feature/task2")  # adopted, not re-branched
        self.assertEqual(gate(wt, "Edit", {"file_path": str(wt / "app.py")}).returncode, 2)
        # main checkout is unaffected by the other worktree's feature
        sh(self.repo, "git", "checkout", "-qb", "feature/other")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)


class TestDiagrams(Repo):
    def test_full_track_requires_valid_diagrams(self):
        cli(self.repo, "new", "Diagrammed", "--track", "full")
        d = self.feature_dir()
        r = cli(self.repo, "approve", "spec", check=False)
        self.assertIn("needs at least one mermaid diagram", r.stderr)
        (d / "spec.md").write_text("# Spec\n```mermaid\nflowchart TD\n  A[parse(x)] --> end\n```\n")
        r = cli(self.repo, "approve", "spec", check=False)
        self.assertIn("quote the label", r.stderr)
        (d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "user: yes")
        (d / "plan.md").write_text("# Plan, no diagram\n")
        self.assertIn("needs at least one", cli(self.repo, "approve", "plan", check=False).stderr)

    def test_lite_plan_diagram_optional_but_linted(self):
        cli(self.repo, "new", "Small")
        d = self.feature_dir()
        (d / "plan.md").write_text("# Plan\n```mermaid\nflowchart LR\n  A:::ghost --> B\n```\n")
        self.assertIn("no classDef", cli(self.repo, "approve", "plan", check=False).stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")

    def test_check_diagrams_cli_and_shipped_templates(self):
        ok = cli(self.repo, "check-diagrams", str(PLUGIN / "references" / "diagrams.md"),
                 str(PLUGIN / "README.md"))
        self.assertIn("lint clean", ok.stdout)
        bad = self.repo / "bad.md"
        bad.write_text("```mermaid\nsequenceDiagram\n  alt x\n  A->>B: hi\n```\n")
        self.assertIn("blocks vs 0 end", cli(self.repo, "check-diagrams", str(bad), check=False).stderr)


LEAN = PLUGIN / "hooks" / "lean.py"


def lean(tool, tool_input, env=None):
    event = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": tool_input}
    r = subprocess.run([sys.executable, str(LEAN)], input=json.dumps(event),
                       env=env or ENV, capture_output=True, text=True)
    assert r.returncode == 0
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""


NOISY_PY = """# Import os
import os
# Define the function
def total(xs):
    # Initialize the sum
    s = 0
    # Loop over items
    for x in xs:
        # Add x to s
        s += x
    # Return the sum
    return s
"""
CLEAN_PY = """import os


def total(xs):
    # Decimal, not float: statement amounts must round like the bank does.
    return sum(xs, start=Decimal(0))


def load(path):
    with open(path) as f:
        return [Decimal(line) for line in f if line.strip()]
"""


class TestLeanHook(Repo):
    def test_flags_narrating_comment_heavy_code(self):
        msg = lean("Write", {"file_path": "/x/calc.py", "content": NOISY_PY})
        self.assertIn("are comments", msg)
        self.assertIn("restate the code", msg)

    def test_clean_code_and_non_code_files_are_silent(self):
        self.assertEqual(lean("Write", {"file_path": "/x/calc.py", "content": CLEAN_PY}), "")
        self.assertEqual(lean("Write", {"file_path": "/x/README.md", "content": NOISY_PY}), "")

    def test_flags_commented_out_code_in_kotlin_edit(self):
        new = "val a = 1\n// val b = compute(a)\n// println(b);\n// if (a > 0) {\nval c = a\n"
        msg = lean("Edit", {"file_path": "/x/Main.kt", "old_string": "val a = 1", "new_string": new})
        self.assertIn("commented-out code", msg)

    def test_edit_counts_only_added_lines(self):
        old = "\n".join(f"# note {i}" for i in range(10)) + "\nx = 1"
        new = old + "\ny = 2"
        self.assertEqual(lean("Edit", {"file_path": "/x/a.py", "old_string": old, "new_string": new}), "")

    def test_flags_whole_file_rewrite_of_tracked_file(self):
        f = self.repo / "big.py"
        f.write_text("".join(f"v{i} = {i}\n" for i in range(100)))
        sh(self.repo, "git", "add", "big.py")
        sh(self.repo, "git", "commit", "-qm", "big")
        content = f.read_text().replace("v5 = 5", "v5 = 50")
        f.write_text(content)
        msg = lean("Write", {"file_path": str(f), "content": content})
        self.assertIn("use Edit", msg)

    def test_off_switch(self):
        env = {**ENV, "SDLC_LEAN": "off"}
        self.assertEqual(lean("Write", {"file_path": "/x/calc.py", "content": NOISY_PY}, env=env), "")


class TestReviewFindings(Repo):
    """Regression tests for the code-review findings on PR #6."""

    def test_pushed_fresh_branch_is_not_mistaken_for_merged(self):  # finding 1
        sh(self.repo, "git", "checkout", "-qb", "feature/early-push")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/early-push")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git commit -m first"}).returncode, 0)
        out = json.loads(cli(self.repo, "new", "Early push").stdout)
        self.assertEqual(out["branch"], "feature/early-push")
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "checkout", "-qb", "feature/tracks-main", "origin/main")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git commit -m x"}).returncode, 0)

    def test_state_json_cannot_be_edited_to_forge_approval(self):  # finding 3
        cli(self.repo, "new", "Forge")
        d = self.feature_dir()
        state = d / "state.json"
        r = gate(self.repo, "Edit", {"file_path": str(state)})
        self.assertEqual(r.returncode, 2)
        self.assertIn("only by `sdlc.py approve`", r.stderr)
        state.write_text(state.read_text().replace('"approvals": {}', '"approvals": {"plan": {}}'))
        r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})  # shell-forged state
        self.assertEqual(r.returncode, 2)
        self.assertIn("changed outside sdlc.py", r.stderr)
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(d / "plan.md")}).returncode, 0)

    def test_lint_accepts_valid_mermaid(self):  # finding 6
        import importlib.util
        spec = importlib.util.spec_from_file_location("sdlc", CLI)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ok = {
            "comma classDef": "flowchart LR\n  A:::a --> B:::b\n  classDef a,b fill:#eee",
            "class with ;": "flowchart LR\n  A --> B\n  class A,B foo;\n  classDef foo fill:#eee",
            "box": "sequenceDiagram\n  box Gray Team\n  participant A\n  end\n  A->>A: hi",
            "front matter": "---\ntitle: Flow\n---\nflowchart LR\n  A --> B",
            "xychart": "xychart-beta\n  x-axis [a, b]\n  bar [1, 2]",
        }
        for name, block in ok.items():
            self.assertEqual(m.lint_mermaid(block), [], name)

    def test_new_twice_same_day_does_not_crash_or_strand_a_branch(self):  # finding 7
        cli(self.repo, "new", "Fix login")
        first = self.feature_dir()
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "wip")
        sh(self.repo, "git", "checkout", "-q", "main")
        r = cli(self.repo, "new", "Fix login", check=False)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(sh(self.repo, "git", "branch", "--show-current").stdout.strip(), "main")
        out = json.loads(cli(self.repo, "new", "Fix login again").stdout)
        self.assertNotEqual(out["dir"], str(first.relative_to(self.repo)))

    def test_same_slug_dir_gets_a_suffix(self):  # finding 7 (dir collision on a fresh branch)
        (self.repo / "docs" / "sdlc").mkdir(parents=True)
        import datetime as dt
        (self.repo / "docs" / "sdlc" / f"{dt.date.today().isoformat()}-dup").mkdir()
        out = json.loads(cli(self.repo, "new", "Dup").stdout)
        self.assertTrue(out["dir"].endswith("-dup-2"))

    def test_lean_ignores_c_pointer_writes(self):  # finding 9
        new = "void f(int *out, char *buf) {\n*out = len;\n*buf = 0;\n}\n"
        self.assertEqual(lean("Edit", {"file_path": "/x/f.c", "old_string": "", "new_string": new}), "")

    def test_new_pulls_the_base_before_branching(self):  # finding 10
        other = Path(self.tmp.name) / "other"
        sh(Path(self.tmp.name), "git", "clone", "-q", str(self.remote), str(other))
        (other / "merged.py").write_text("y = 1\n")
        sh(other, "git", "add", ".")
        sh(other, "git", "commit", "-qm", "merged elsewhere")
        sh(other, "git", "push", "-q", "origin", "main")
        cli(self.repo, "new", "After merge")
        self.assertTrue((self.repo / "merged.py").exists())


class TestNoPublishAndEvidence(Repo):
    def test_never_pushes_opens_or_merges_prs(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ("git push -u origin HEAD", "gh pr create --fill"):
            r = gate(self.repo, "Bash", {"command": cmd})
            self.assertEqual(r.returncode, 0, cmd)
            out = json.loads(r.stdout)["hookSpecificOutput"]
            self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "ask"))
            self.assertTrue(out["permissionDecisionReason"])
        for cmd in ("gh pr merge 6 --squash", "git push --force", "git push -f origin HEAD",
                    "git push --force-with-lease", "git push origin +HEAD", "git push origin HEAD:main",
                    "git push origin main", "git push --delete origin x", "git push origin :x",
                    "git push --mirror", "cd . && git push", "cd . && git push && git push --force"):
            r = gate(self.repo, "Bash", {"command": cmd})
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("never", r.stderr)
        sh(self.repo, "git", "checkout", "-q", "main")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git push"}).returncode, 2)
        sh(self.repo, "git", "checkout", "-q", "feature/x")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git merge --no-ff lane-a"}).returncode, 0)
        sh(self.repo, "git", "checkout", "-q", "main")  # merging into the base: refused by git itself
        (self.repo / "m.py").write_text("m = 1\n")
        sh(self.repo, "git", "checkout", "-q", "feature/x")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "x work")
        sh(self.repo, "git", "checkout", "-q", "main")
        r = run(self.repo, "git", "merge", "--no-ff", "-q", "-m", "m", "feature/x", env=claude_env())
        self.assertNotEqual(r.returncode, 0)

    def test_approval_needs_evidence_no_open_questions_and_user_words(self):
        cli(self.repo, "new", "Evidence")
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("None\n", "- which currency?\n"))
        self.assertIn("open question", cli(self.repo, "approve", "plan", "--note", "ok", check=False).stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("app.py:1 sets x = 1", "it probably works"))
        self.assertIn("must cite", cli(self.repo, "approve", "plan", "--note", "ok", check=False).stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        self.assertIn("--note is required", cli(self.repo, "approve", "plan", check=False).stderr)
        cli(self.repo, "approve", "plan", "--note", "user: approved as written")


def load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestSecondReview(Repo):
    """Regression tests for the second code review of PR #6."""

    def test_push_is_blocked_whatever_wraps_it(self):  # findings 1 + 2
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ("timeout 30 git push", "nice -n 5 git push", "sudo -u me git push",
                    "sleep 0 & git push", 'echo "$(git push)"', "bash -c 'git push origin HEAD'",
                    "git -C . push", "gh  pr  merge 2", 'sh -c "gh pr merge 1"'):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 2, cmd)
        for cmd in ("git stash push -m wip", "git log --oneline", "echo pushing"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 0, cmd)
        msg = "git commit -F - <<'EOF'\nnever git push from the SDLC\nEOF"
        self.assertEqual(gate(self.repo, "Bash", {"command": msg}).returncode, 0)

    def test_wrapped_commit_on_main_is_still_checked(self):  # findings 1 + 2
        for cmd in ("timeout 30 git commit -m x", "sleep 0 & git commit -m x"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 2, cmd)

    def test_reapproving_changed_spec_reopens_later_gates(self):  # finding 3
        cli(self.repo, "new", "Reopen", "--track", "full")
        d = self.feature_dir()
        (d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "yes")
        (d / "plan.md").write_text(GOOD_PLAN)
        cli(self.repo, "approve", "plan", "--note", "yes")
        cli(self.repo, "approve", "build", "--evidence", "ok")
        (d / "spec.md").write_text(GOOD_SPEC.replace("AC1: given x, then y", "AC1: given x, then z"))
        st = json.loads(cli(self.repo, "approve", "spec", "--note", "yes, new AC").stdout)
        self.assertEqual(st["phase"], "plan")
        self.assertEqual((st["gates"]["plan"], st["gates"]["build"]), ("open", "open"))

    def test_dropping_to_lite_needs_user_and_reopens_plan(self):  # finding 4
        cli(self.repo, "new", "Downgrade", "--track", "full")
        d = self.feature_dir()
        (d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "yes")
        (d / "plan.md").write_text(GOOD_PLAN)
        cli(self.repo, "approve", "plan", "--note", "yes")
        self.assertIn("--note", cli(self.repo, "track", "lite", check=False).stderr)
        st = json.loads(cli(self.repo, "track", "lite", "--note", "user: lite is fine").stdout)
        self.assertEqual(st["phase"], "plan")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)

    def test_old_merged_pr_on_a_recreated_branch_is_ignored(self):  # finding 5
        m = load_module(CLI, "sdlc_mod")
        prs = [{"number": 7, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}]
        created_after = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc).timestamp()
        created_before = dt.datetime(2025, 12, 1, tzinfo=dt.timezone.utc).timestamp()
        self.assertIsNone(m.pick_merged(prs, created_after))
        self.assertEqual(m.pick_merged(prs, created_before), 7)
        self.assertIsNone(m.pick_merged(prs + [{"number": 9, "state": "OPEN"}], created_before))
        sh(self.repo, "git", "checkout", "-qb", "feature/stamp")
        self.assertIsNotNone(m._branch_created(self.repo, "feature/stamp"))

    def test_quoted_arrows_are_not_writes(self):  # finding 6
        cli(self.repo, "new", "Arrows")
        for cmd in ('git commit -m "spec -> plan"', 'echo "a > b"', "echo 'x >> y'"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 0, cmd)


    def test_new_honours_base_from_main(self):  # finding 7
        sh(self.repo, "git", "checkout", "-qb", "develop")
        (self.repo / "dev_only.py").write_text("d = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "develop work")
        sh(self.repo, "git", "push", "-qu", "origin", "develop")
        sh(self.repo, "git", "checkout", "-q", "main")
        out = json.loads(cli(self.repo, "new", "On develop", "--base", "develop").stdout)
        self.assertEqual(out["base"], "develop")
        self.assertTrue((self.repo / "dev_only.py").exists())
        upstream = sh(self.repo, "git", "rev-parse", "--abbrev-ref", "@{u}", check=False)
        self.assertNotEqual(upstream.returncode, 0)  # not tracking origin/develop

    def test_merge_on_github_detected_without_manual_fetch(self):  # finding 8
        sh(self.repo, "git", "checkout", "-qb", "feature/m")
        (self.repo / "app.py").write_text("x = 9\n")
        sh(self.repo, "git", "commit", "-qam", "change")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/m")
        other = Path(self.tmp.name) / "gh-side"
        sh(Path(self.tmp.name), "git", "clone", "-q", str(self.remote), str(other))
        sh(other, "git", "merge", "-q", "--ff-only", "origin/feature/m")
        sh(other, "git", "push", "-q", "origin", "main", ":feature/m")
        r = gate(self.repo, "Bash", {"command": "git commit -m more"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("merged", r.stderr)

    def test_review_template_is_not_a_pass(self):  # finding 9
        tpl = (PLUGIN / "skills" / "sdlc-review" / "SKILL.md").read_text()
        cli(self.repo, "new", "Tpl")
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "yes")
        cli(self.repo, "approve", "build", "--evidence", "ok")
        (d / "review.md").write_text(tpl.split("~~~markdown")[-1] if "~~~markdown" in tpl
                                     else tpl.split("```markdown")[-1])
        self.assertIn("Verdict: PASS", cli(self.repo, "approve", "review", check=False).stderr)

    def test_router_ignores_noun_forms(self):  # finding 10
        for p in ("List the developer settings in this repo", "Summarize the implementation notes",
                  "who refactored this last year?"):
            self.assertEqual(route(self.repo, p).stdout, "", p)
        for p in ("Implement CSV export", "please refactor the parser", "can you develop a widget"):
            self.assertIn("sdlc skill", route(self.repo, p).stdout, p)


class TestThirdReview(Repo):
    """Regression tests for the third code review of PR #6 (reviewer's own repros)."""

    def blocked(self, cmd, cwd=None):
        return gate(cwd or self.repo, "Bash", {"command": cmd}).returncode == 2

    def test_pushes_the_regex_missed(self):  # finding 1
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ('git -c user.name="A B" push origin feat',
                    "gh -R o/r pr create", "gh --repo o/r pr merge 3"):
            self.assertTrue(self.blocked(cmd), cmd)

    def test_absolute_path_git(self):  # finding 2
        self.assertTrue(self.blocked("/usr/bin/git commit -m x"))  # on main
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.assertTrue(self.blocked("/opt/homebrew/bin/git push origin feat"))

    def test_here_strings_and_shifts_do_not_hide_lines(self):  # finding 3
        self.assertTrue(self.blocked("cat <<< hi\ngit commit -m x"))
        self.assertTrue(self.blocked("echo $((a<<b))\ngit commit -m x"))
        self.assertTrue(self.blocked("cat <<EOF\nno terminator\ngit commit -m x"))

    def test_bsd_sed_on_plan_docs_is_allowed(self):  # finding 7
        cli(self.repo, "new", "Sed")
        d = self.status()["dir"]
        self.assertFalse(self.blocked(f"sed -i '' 's/a/b/' {d}/plan.md"))
        self.assertFalse(self.blocked(f"sed -i.bak -e 's/a/b/' -e 's/c/d/' {d}/plan.md"))

    def test_gate_hook_blocks_on_timeout_and_has_budget(self):  # finding 8
        cfg = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        h = cfg["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(h["onFailure"], "block")
        self.assertGreaterEqual(h["timeout"], 30)
        src = CLI.read_text()
        self.assertIn('timeout=4', src.split("def _merged_pr")[1].split("def ")[0])
        self.assertIn('"--prune", "-q", "origin", timeout=4', src)

    def test_failed_fetch_is_throttled(self):  # finding 8
        m = load_module(CLI, "sdlc_fetch")
        sh(self.repo, "git", "remote", "set-url", "origin", "/nonexistent/remote.git")
        m.refresh_remote(self.repo)
        stamp = self.repo / ".git" / "sdlc-fetch-stamp"
        self.assertTrue(stamp.exists())
        before = stamp.stat().st_mtime
        m.refresh_remote(self.repo)
        self.assertEqual(stamp.stat().st_mtime, before)

    def test_lint_header_semicolon(self):  # finding 9
        m = load_module(CLI, "sdlc_lint")
        self.assertEqual(m.lint_mermaid("graph TD;\n  A-->B"), [])
        self.assertEqual(m.lint_mermaid("flowchart LR;\n  A-->B"), [])

    def test_evidence_and_none_are_strict(self):  # finding 10
        cli(self.repo, "new", "Strict")
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("app.py:1 sets x = 1", "I checked, e.g. the loader"))
        self.assertIn("must cite", cli(self.repo, "approve", "plan", "--note", "ok", check=False).stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("None\n", "None of these are decided: should we cache?\n"))
        self.assertIn("open question", cli(self.repo, "approve", "plan", "--note", "ok", check=False).stderr)
        for ok in ("- src/loader/io.py:12 opens the file", "- `uv run pytest -q` -> 3 failed", "- nothing existing"):
            (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("- app.py:1 sets x = 1", ok))
            r = cli(self.repo, "approve", "plan", "--note", "ok", check=False)
            self.assertEqual(r.returncode, 0, (ok, r.stderr))
            cli(self.repo, "reopen", "plan")


GITHOOKS = PLUGIN / "githooks"
SESSION = PLUGIN / "hooks" / "session.py"
DRIFT = PLUGIN / "hooks" / "drift.py"


def claude_env(**extra):
    """Environment of a Claude Code Bash command with the session hooks installed."""
    env = {**ENV, "CLAUDECODE": "1", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
           "GIT_CONFIG_VALUE_0": str(GITHOOKS), "SDLC_NO_FETCH": "1"}
    env.pop("SDLC_ALLOW_PUSH", None)
    env.update(extra)
    return env


def run(cwd, *args, env):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


class TestGitHooks(Repo):
    """Git-level enforcement: holds however the command is spelled."""

    def test_push_refused_in_claude_sessions_only(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        env = claude_env()
        for args in (["git", "push", "-q", "origin", "HEAD"],
                     ["/usr/bin/git", "push", "-q", "origin", "HEAD"],
                     ["git", "-c", "alias.p=push", "p", "-q", "origin", "HEAD"],
                     ["sh", "-c", "timeout_free=1 git push -q origin HEAD"]):
            r = run(self.repo, *args, env=env)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("never pushes", r.stderr, args)
        self.assertEqual(run(self.repo, "git", "push", "-q", "origin", "HEAD",
                             env=claude_env(SDLC_ALLOW_PUSH="1")).returncode, 0)
        user_env = {k: v for k, v in env.items() if k != "CLAUDECODE"}
        sh(self.repo, "git", "push", "-q", "origin", "--delete", "feature/x")
        self.assertEqual(run(self.repo, "git", "push", "-q", "origin", "HEAD", env=user_env).returncode, 0)

    def test_pre_commit_checks_branch_and_plan(self):
        env = claude_env()
        (self.repo / "app.py").write_text("x = 2\n")
        r = run(self.repo, "git", "commit", "-qam", "on main", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("main", r.stderr)
        sh(self.repo, "git", "checkout", "-q", "app.py")
        cli(self.repo, "new", "Hooked")
        d = self.feature_dir()
        run(self.repo, "git", "add", "-A", env=env)
        self.assertEqual(run(self.repo, "git", "commit", "-qm", "docs only", env=env).returncode, 0)
        (self.repo / "app.py").write_text("x = 3\n")
        r = run(self.repo, "git", "commit", "-qam", "code", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no approved plan", r.stderr)
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        run(self.repo, "git", "add", "-A", env=env)
        self.assertEqual(run(self.repo, "git", "commit", "-qm", "code", env=env).returncode, 0)

    def test_chains_to_the_repos_own_hooks(self):
        env = claude_env()
        sh(self.repo, "git", "checkout", "-qb", "feature/chain")
        own = self.repo / ".git" / "hooks"
        (own / "pre-commit").write_text("#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/.ran-pre-commit\"\n")
        (own / "commit-msg").write_text("#!/bin/sh\ngrep -q ticket \"$1\" || { echo needs ticket >&2; exit 1; }\n")
        for h in own.iterdir():
            h.chmod(0o755)
        (self.repo / "b.py").write_text("b = 1\n")
        run(self.repo, "git", "add", "b.py", env=env)
        r = run(self.repo, "git", "commit", "-qm", "no id", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs ticket", r.stderr)
        self.assertEqual(run(self.repo, "git", "commit", "-qm", "ticket 1", env=env).returncode, 0)
        self.assertTrue((self.repo / ".ran-pre-commit").exists())
        custom = Path(self.tmp.name) / "custom-hooks"
        custom.mkdir()
        (custom / "post-commit").write_text("#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/.ran-custom\"\n")
        (custom / "post-commit").chmod(0o755)
        sh(self.repo, "git", "config", "--local", "core.hooksPath", str(custom))
        (self.repo / "c.py").write_text("c = 1\n")
        run(self.repo, "git", "add", "c.py", env=env)
        self.assertEqual(run(self.repo, "git", "commit", "-qm", "ticket 2", env=env).returncode, 0)
        self.assertTrue((self.repo / ".ran-custom").exists())


class TestSessionInstaller(Repo):
    def test_env_file_routes_git_through_plugin_hooks(self):
        envf = Path(self.tmp.name) / "env.sh"
        r = subprocess.run([sys.executable, str(SESSION)], env={**ENV, "CLAUDE_ENV_FILE": str(envf)},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        base_env = {k: v for k, v in ENV.items() if not k.startswith(("GIT_CONFIG", "SDLC_GITHOOKS"))}
        probe = f". {envf}; . {envf}; echo $GIT_CONFIG_COUNT; git config --get core.hooksPath"
        out = subprocess.run(["sh", "-c", probe], cwd=self.repo, env=base_env,
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["1", str(GITHOOKS)])  # sourced twice, installed once
        out = subprocess.run(["sh", "-c", f". {envf}; echo $GIT_CONFIG_COUNT"], cwd=self.repo,
                             env={**base_env, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name",
                                  "GIT_CONFIG_VALUE_0": "x"}, capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["2"])  # appends after existing command-scope config
        off = subprocess.run(["sh", "-c", f". {envf}; echo ${{GIT_CONFIG_COUNT:-none}}"], cwd=self.repo,
                             env={**base_env, "SDLC_GATES": "off"}, capture_output=True, text=True).stdout.split()
        self.assertEqual(off, ["none"])
        self.assertTrue(all(os.access(f, os.X_OK) for f in GITHOOKS.iterdir()))


class TestDriftAndState(Repo):
    def drift_msg(self):
        ev = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(self.repo)}
        r = subprocess.run([sys.executable, str(DRIFT)], input=json.dumps(ev), env=ENV,
                           capture_output=True, text=True)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""

    def test_unapproved_code_changes_are_reported_but_old_dirt_is_not(self):
        (self.repo / "scratch.txt").write_text("pre-existing\n")  # dirty before the feature
        cli(self.repo, "new", "Drift")
        self.assertEqual(self.drift_msg(), "")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "sneaky.py").write_text("x = 1\n")  # e.g. python -c wrote it
        (self.repo / "app.py").write_text("x = 99\n")
        msg = self.drift_msg()
        self.assertIn("src/sneaky.py", msg)
        self.assertIn("app.py", msg)
        self.assertNotIn("scratch.txt", msg)
        (self.feature_dir() / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        self.assertEqual(self.drift_msg(), "")

    def test_corrupt_state_blocks_instead_of_disabling(self):
        cli(self.repo, "new", "Corrupt")
        state = self.feature_dir() / "state.json"
        for bad in ('<<<<<<< HEAD\n{"title": "a"}\n=======\n', '{"title": "x", "branch": "b", "approvals": null}'):
            state.write_text(bad)
            r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("unreadable", cli(self.repo, "status", check=False).stderr)

    def test_deleted_feature_folder_still_gates(self):
        cli(self.repo, "new", "Vanish")
        d = self.feature_dir()
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        import shutil
        shutil.rmtree(d)  # e.g. done by a command the text gate didn't understand
        r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})
        self.assertEqual(r.returncode, 2)
        self.assertIn("deleted", cli(self.repo, "status", check=False).stderr)


class TestFourthReview(Repo):
    def blocked(self, cmd):
        return gate(self.repo, "Bash", {"command": cmd}).returncode == 2

    def test_shell_fed_heredoc_push_is_seen(self):  # finding 3
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.assertTrue(self.blocked("bash <<'EOF'\ngit push origin HEAD\nEOF"))
        self.assertFalse(self.blocked("git commit -F - <<'EOF'\nmention git push here\nEOF"))

    def test_aliases_gh_api_and_hook_evasion(self):  # finding 5
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        sh(self.repo, "git", "config", "alias.p", "push")
        sh(self.repo, "git", "config", "alias.sync", "!git fetch && git push")
        for cmd in ("gh api repos/o/r/pulls -f title=x",
                    "gh api -X PUT repos/o/r/pulls/3/merge", "git commit --no-verify -m x",
                    "git -c core.hooksPath=/dev/null commit -m x", "GIT_CONFIG_COUNT=0 git commit -m x",
                    "unset GIT_CONFIG_COUNT; git commit -m x"):
            self.assertTrue(self.blocked(cmd), cmd)
        self.assertFalse(self.blocked("gh api repos/o/r/pulls -X GET"))
        self.assertFalse(self.blocked("git st"))

    def test_internal_error_on_git_command_fails_closed(self):  # finding 7
        ev = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": 123}
        r = subprocess.run([sys.executable, str(GATE)], input=json.dumps(ev), env=ENV,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        ev["tool_input"]["command"] = "ls"
        r = subprocess.run([sys.executable, str(GATE)], input=json.dumps(ev), env=ENV,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)

    def test_router_with_leading_newline(self):  # nit
        self.assertIn("sdlc skill", route(self.repo, "\nimplement caching").stdout)


class TestFifthReview(Repo):
    def blocked(self, cmd):
        return gate(self.repo, "Bash", {"command": cmd}).returncode == 2

    def approved(self, title="Approved"):
        cli(self.repo, "new", title)
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        return d

    def test_tree_commands_work_once_plan_is_approved(self):  # finding 1
        self.approved()
        for cmd in ("git merge --no-ff --no-edit lane-a", "git merge --abort", "git pull",
                    "git apply p.diff", "git stash pop", "git reset --hard HEAD", "git checkout -- ."):
            self.assertFalse(self.blocked(cmd), cmd)

    def test_case_variants_of_state_json(self):  # finding 2
        cli(self.repo, "new", "Case")
        d = self.status()["dir"]
        for name in ("State.json", "STATE.JSON"):
            self.assertEqual(gate(self.repo, "Write", {"file_path": str(self.repo / d / name)}).returncode, 2)

    def test_inline_env_cannot_switch_gates_off(self):  # finding 3
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ("CLAUDECODE= git commit -am wip", "SDLC_GATES=off git commit -am wip",
                    "SDLC_ALLOW_PUSH=1 git push", "env -i git commit -m x", "env -u CLAUDECODE git push",
                    "export SDLC_GATES=off", "unset CLAUDECODE", "bash -c 'SDLC_GATES=off git commit -m x'"):
            self.assertTrue(self.blocked(cmd), cmd)
        self.assertFalse(self.blocked('SDLC="python3 sdlc.py"; $SDLC status'))

    def test_reopened_plan_keeps_approved_work(self):  # finding 4
        d = self.approved()
        drift_ev = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(self.repo)}
        run_drift = lambda: subprocess.run([sys.executable, str(DRIFT)], input=json.dumps(drift_ev),
                                           env=ENV, capture_output=True, text=True).stdout
        (self.repo / "app.py").write_text("x = 'built under approval'\n")
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG.replace("do it", "do it better"))  # gate re-opens
        self.assertEqual(run_drift(), "")  # build work is not reported
        (self.repo / "new_after_reopen.py").write_text("y = 1\n")
        self.assertIn("new_after_reopen.py", run_drift())
        self.assertNotIn("app.py", run_drift())

    def test_quoted_mentions_are_data(self):  # finding 5 (nit)
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ('grep -rn "git push" .', 'git commit -m "docs: never git push"', "rg 'gh pr create'"):
            self.assertFalse(self.blocked(cmd), cmd)
        for cmd in ('echo "$(git push)"', "bash -c 'git push'", "eval 'git push'"):
            self.assertTrue(self.blocked(cmd), cmd)

    def test_base_branch_ref_cannot_be_moved(self):  # finding 6 (nit)
        env = claude_env()
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        (self.repo / "w.py").write_text("w = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "work")
        for args in (["git", "branch", "-f", "main", "HEAD"], ["git", "update-ref", "refs/heads/main", "HEAD"]):
            r = run(self.repo, *args, env=env)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertNotEqual(sh(self.repo, "git", "rev-parse", "main").stdout,
                            sh(self.repo, "git", "rev-parse", "HEAD").stdout)
        other = Path(self.tmp.name) / "upstream"  # fast-forward to origin/main stays allowed
        sh(Path(self.tmp.name), "git", "clone", "-q", str(self.remote), str(other))
        (other / "u.py").write_text("u = 1\n")
        sh(other, "git", "add", ".")
        sh(other, "git", "commit", "-qm", "upstream")
        sh(other, "git", "push", "-q", "origin", "main")
        sh(self.repo, "git", "checkout", "-q", "main")
        self.assertEqual(run(self.repo, "git", "pull", "-q", "--ff-only", env=env).returncode, 0)

    def test_rename_entries_parse_cleanly(self):  # finding 7 (nit)
        m = load_module(CLI, "sdlc_ren")
        cli(self.repo, "new", "Rename")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        sh(self.repo, "git", "mv", "app.py", "renamed.py")
        dirty = m.dirty_code(self.repo)
        self.assertEqual(sorted(dirty), ["app.py", "renamed.py"])
        self.assertEqual(dirty["app.py"], "")  # renamed away = deleted at its old path


class TestSixthReview(Repo):
    def blocked(self, cmd, cwd=None):
        return gate(cwd or self.repo, "Bash", {"command": cmd}).returncode == 2

    def test_drift_revert_path_works_and_is_scoped(self):  # finding 1
        (self.repo / "keep.txt").write_text("user's pre-existing edit\n")
        cli(self.repo, "new", "Revert")
        (self.repo / "app.py").write_text("x = 'unapproved'\n")
        (self.repo / "new.py").write_text("n = 1\n")
        (self.repo / "keep.txt").write_text("changed after baseline\n")
        self.assertIn("revert-drift", subprocess.run(
            [sys.executable, str(DRIFT)], input=json.dumps({"tool_name": "Bash", "cwd": str(self.repo)}),
            env=ENV, capture_output=True, text=True).stdout)
        self.assertIn("isn't in the drift list", cli(self.repo, "revert-drift", "README.md", check=False).stderr)
        cli(self.repo, "revert-drift", "app.py", "new.py", "keep.txt")
        self.assertEqual((self.repo / "app.py").read_text(), "x = 1\n")
        self.assertFalse((self.repo / "new.py").exists())
        self.assertEqual((self.repo / "keep.txt").read_text(), "user's pre-existing edit\n")
        self.assertFalse(self.blocked(f"python3 {CLI} revert-drift app.py"))

    def test_deleted_uncommitted_feature_fails_closed(self):  # finding 2
        cli(self.repo, "new", "Untracked")
        import shutil
        shutil.rmtree(self.feature_dir())  # however it happened
        r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})
        self.assertEqual(r.returncode, 2)
        self.assertIn("--note", cli(self.repo, "abandon", check=False).stderr)
        cli(self.repo, "abandon", "--note", "user: drop it")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)

    def test_alias_cannot_carry_hook_overrides(self):  # finding 4
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        sh(self.repo, "git", "config", "alias.ci", "commit")
        self.assertTrue(self.blocked("git -c core.hooksPath=/dev/null ci -m x"))
        self.assertTrue(self.blocked("git -c core.hooksPath=/dev/null status"))

    def test_quoted_env_names_are_data(self):  # finding 5
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ('grep -n "SDLC_GATES=off" README.md', 'git commit -m "document SDLC_ALLOW_PUSH=1"',
                    "rg 'CLAUDECODE=1' docs"):
            self.assertFalse(self.blocked(cmd), cmd)
        for cmd in ("SDLC_GATES=off git commit -m x", "bash -c 'SDLC_GATES=off git commit -m x'",
                    "export GIT_CONFIG_COUNT=0", "env --unset=CLAUDECODE git push"):
            self.assertTrue(self.blocked(cmd), cmd)

    def test_fork_upstream_fast_forward_is_allowed(self):  # finding 6
        up = Path(self.tmp.name) / "upstream.git"
        sh(Path(self.tmp.name), "git", "clone", "-q", "--bare", str(self.remote), str(up))
        sh(self.repo, "git", "remote", "add", "upstream", str(up))
        other = Path(self.tmp.name) / "up-work"
        sh(Path(self.tmp.name), "git", "clone", "-q", str(up), str(other))
        (other / "f.py").write_text("f = 1\n")
        sh(other, "git", "add", ".")
        sh(other, "git", "commit", "-qm", "upstream change")
        sh(other, "git", "push", "-q", "origin", "main")
        env = claude_env()
        self.assertEqual(run(self.repo, "git", "pull", "-q", "--ff-only", "upstream", "main", env=env).returncode, 0)

    def test_handoff_push_is_allowed_for_the_user(self):  # finding 10
        sh(self.repo, "git", "checkout", "-qb", "feature/handoff")
        r = run(self.repo, "git", "push", "-q", "-u", "origin", "feature/handoff",
                env=claude_env(SDLC_ALLOW_PUSH="1"))  # what `! SDLC_ALLOW_PUSH=1 git push` does
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SDLC_ALLOW_PUSH=1", (PLUGIN / "skills" / "sdlc-ship" / "SKILL.md").read_text())


class TestSeventhReview(Repo):
    def blocked(self, cmd):
        return gate(self.repo, "Bash", {"command": cmd}).returncode == 2

    def test_pull_is_allowed_for_recovery(self):  # finding 1
        self.assertFalse(self.blocked("git checkout main && git pull"))
        self.assertFalse(self.blocked("git pull --ff-only"))

    def test_plumbing_is_not_commit_or_merge(self):  # finding 2
        for cmd in ("git merge-base main HEAD", "git merge-tree a b", "git commit-tree HEAD^{tree} -m x",
                    "git commit-graph write"):
            self.assertFalse(self.blocked(cmd), cmd)  # on main
        self.assertTrue(self.blocked("git commit -m x"))

    def test_done_feature_with_deleted_folder_does_not_block(self):  # finding 3
        cli(self.repo, "new", "Shipped")
        d = self.feature_dir()
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "yes")
        cli(self.repo, "approve", "build", "--evidence", "ok")
        (d / "review.md").write_text("# Review\n## Verdict: PASS\n")
        cli(self.repo, "approve", "review")
        (d / "pr.md").write_text("PR\n")
        cli(self.repo, "approve", "ship")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "shipped")
        import shutil
        shutil.rmtree(d)
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)

    def test_quoted_hook_words_are_data(self):  # finding 5
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        for cmd in ('git commit -m "mention core.hooksPath fix"', "git log --grep=--no-verify",
                    "git log --grep='GIT_CONFIG_COUNT'"):
            self.assertFalse(self.blocked(cmd), cmd)
        for cmd in ('git -c "core.hooksPath=/dev/null" commit -m x', 'git commit "--no-verify" -m x'):
            self.assertTrue(self.blocked(cmd), cmd)

    def test_abandon_reports_truthfully(self):  # finding 6
        cli(self.repo, "new", "Gone")
        d = self.feature_dir()
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        import shutil
        shutil.rmtree(d)
        cli(self.repo, "abandon", "--note", "user: drop it")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)
        cli(self.repo, "new", "Broken")
        (self.feature_dir() / "state.json").write_text("{nope")
        self.assertIn("unreadable", cli(self.repo, "abandon", "--note", "x", check=False).stderr)

    def test_many_untracked_files_hash_in_one_process(self):  # finding 8
        m = load_module(CLI, "sdlc_many")
        for i in range(300):
            (self.repo / f"gen_{i}.txt").write_text(str(i))
        import time
        t = time.time()
        dirty = m.dirty_code(self.repo)
        self.assertEqual(len(dirty), 300)
        self.assertTrue(all(len(h) == 40 for h in dirty.values()))
        self.assertLess(time.time() - t, 5)

    def test_gh_api_reads_are_allowed(self):  # finding 9
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.assertFalse(self.blocked("gh api repos/o/r/pulls/3/comments"))
        self.assertFalse(self.blocked("gh api repos/o/r/pulls --paginate"))
        self.assertTrue(self.blocked("gh api repos/o/r/pulls -f title=x -f head=b"))
        self.assertTrue(self.blocked("gh api --method PUT repos/o/r/pulls/3/merge"))

    def test_readme_names_aliases_not_versions(self):  # finding 10
        self.assertNotRegex((PLUGIN / "README.md").read_text(), r"(?:Opus|Sonnet|Haiku) \d")


class TestAskBeforePublish(Repo):
    def setUp(self):
        super().setUp()
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.token = self.repo / ".git" / "sdlc-push-ok"

    def ask(self, cmd="git push -u origin HEAD"):
        r = gate(self.repo, "Bash", {"command": cmd})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def push(self, *extra):
        return run(self.repo, "git", "push", "-q", "-u", "origin", "HEAD", *extra, env=claude_env())

    def commit(self, name="n.py"):
        (self.repo / name).write_text("n = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", name)

    def test_token_is_written_on_ask_and_bound_to_sha_and_ref(self):
        r = self.ask()
        tok = json.loads(self.token.read_text())
        head = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(tok["pushes"], [{"ref": "refs/heads/feature/x", "sha": head}])
        self.assertTrue(55 < tok["expires"] - dt.datetime.now().timestamp() <= 60)
        self.assertIn("feature/x", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_pr_create_asks_without_a_push_token(self):
        self.ask("gh pr create --fill")
        self.assertFalse(self.token.exists())

    def test_prepush_allows_an_approved_push_once(self):
        self.ask()
        self.assertEqual(self.push().returncode, 0)
        self.assertFalse(self.token.exists())
        self.commit()
        r = self.push()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never pushes", r.stderr)

    def test_prepush_refuses_without_a_token(self):
        self.assertNotEqual(self.push().returncode, 0)

    def test_prepush_refuses_an_expired_token(self):
        self.ask()
        tok = json.loads(self.token.read_text())
        tok["expires"] -= 120
        self.token.write_text(json.dumps(tok))
        self.assertNotEqual(self.push().returncode, 0)

    def test_prepush_refuses_when_the_sha_differs(self):
        self.ask()
        self.commit()
        self.assertNotEqual(self.push().returncode, 0)

    def test_prepush_refuses_a_different_ref(self):
        self.ask()
        r = run(self.repo, "git", "push", "-q", "origin", "HEAD:refs/heads/other", env=claude_env())
        self.assertNotEqual(r.returncode, 0)

    def test_drift_removes_a_leftover_token(self):
        self.ask()
        subprocess.run([sys.executable, str(DRIFT)], env=ENV, capture_output=True, text=True,
                       input=json.dumps({"tool_name": "Bash", "cwd": str(self.repo)}))
        self.assertFalse(self.token.exists())

    def test_inline_allow_push_is_still_refused(self):
        r = gate(self.repo, "Bash", {"command": "SDLC_ALLOW_PUSH=1 git push"})
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.token.exists())


class TestV2Layers(Repo):
    """v2: shell writes are caught by results (drift, tamper, git hooks), not by parsing."""

    def drift(self):
        return subprocess.run([sys.executable, str(DRIFT)], env=ENV, capture_output=True, text=True,
                              input=json.dumps({"tool_name": "Bash", "cwd": str(self.repo)})).stdout

    def test_any_shell_write_form_is_reported_by_drift(self):
        cli(self.repo, "new", "Forms")
        for i, cmd in enumerate(["sed -i.bak s/1/2/ app.py", "bash -lc 'echo x > b.py'",
                                 "cat <<EOF | sh\necho y > c.py\nEOF", "printf z >d.py"]):
            subprocess.run(["sh", "-c", cmd], cwd=self.repo, env=ENV, capture_output=True)
        out = self.drift()
        for f in ("app.py", "b.py", "c.py", "d.py"):
            self.assertIn(f, out)

    def test_cherry_pick_onto_merged_branch_refused_by_git(self):
        env = claude_env()
        sh(self.repo, "git", "checkout", "-qb", "feature/m")
        (self.repo / "app.py").write_text("x = 5\n")
        sh(self.repo, "git", "commit", "-qam", "work")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/m")
        sh(self.repo, "git", "push", "-q", "origin", "feature/m:main")  # merged on the server
        sh(self.repo, "git", "fetch", "-q")
        sh(self.repo, "git", "checkout", "-qb", "side", "main")
        (self.repo / "s.py").write_text("s = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "side")
        pick = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        sh(self.repo, "git", "checkout", "-q", "feature/m")
        r = run(self.repo, "git", "cherry-pick", pick, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("merged", r.stderr)

    def test_push_to_base_refused_even_when_pushing_is_allowed(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        (self.repo / "p.py").write_text("p = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "new work")
        r = run(self.repo, "git", "push", "-q", "origin", "HEAD:main", env=claude_env(SDLC_ALLOW_PUSH="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only via a PR", r.stderr)

    def test_legit_checkout_of_committed_state_is_not_tamper(self):
        cli(self.repo, "new", "Legit")
        d = self.feature_dir()
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        (d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "yes")  # state.json now differs from HEAD
        sh(self.repo, "git", "checkout", "-q", "--", str(d / "state.json"))  # back to HEAD's copy
        self.assertEqual(self.status()["phase"], "plan")  # trusted: it's the committed version

    def test_gate_is_small(self):
        self.assertLess(len(GATE.read_text().splitlines()), 260)


class TestFinalReview(Repo):
    """One-pass final review (v2), batch-fixed."""

    def blocked(self, cmd):
        return gate(self.repo, "Bash", {"command": cmd}).returncode == 2

    def test_commits_that_skip_pre_commit_are_checked_by_git(self):  # 1
        env = claude_env()
        cli(self.repo, "new", "Unapproved")
        sh(self.repo, "git", "add", "-A")
        run(self.repo, "git", "commit", "-qm", "docs", env=env)
        sh(self.repo, "git", "checkout", "-qb", "donor")  # descends from the feature tip
        (self.repo / "donor.py").write_text("d = 1\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "donor")
        pick = sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        sh(self.repo, "git", "checkout", "-q", "feature/unapproved")
        for args in (["git", "cherry-pick", pick], ["git", "merge", "--ff-only", "donor"]):
            r = run(self.repo, *args, env=env)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("no approved plan", r.stderr, args)
            run(self.repo, "git", "cherry-pick", "--abort", env=env)
        (self.repo / "app.py").write_text("x = 7\n")
        r = run(self.repo, "git", "commit", "-qam", "skip hooks", "-n", env=env)
        self.assertNotEqual(r.returncode, 0)

    def test_clean_refused_while_feature_docs_are_uncommitted(self):  # 2
        cli(self.repo, "new", "Docs")
        for cmd in ("git clean -fd", "git stash -u", "git stash push --include-untracked"):
            self.assertTrue(self.blocked(cmd), cmd)
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        self.assertFalse(self.blocked("git clean -fd"))

    def test_unrelated_branch_does_not_inherit_a_feature(self):  # 3, 4
        cli(self.repo, "new", "A")  # feature/a at main's tip, no commits yet
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "checkout", "-qb", "fix/typo")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 0)
        out = json.loads(cli(self.repo, "new", "Typo").stdout)
        self.assertEqual(out["branch"], "fix/typo")

    def test_detached_head_inside_a_feature_cannot_change_it(self):  # 4
        cli(self.repo, "new", "Det")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        sh(self.repo, "git", "checkout", "-q", "--detach")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)
        self.assertIn("check it out", cli(self.repo, "abandon", "--note", "x", check=False).stderr)

    def test_branch_from_merged_remote_branch_is_stale(self):  # 6
        sh(self.repo, "git", "checkout", "-qb", "feature/r")
        (self.repo / "app.py").write_text("x = 3\n")
        sh(self.repo, "git", "commit", "-qam", "r")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/r")
        sh(self.repo, "git", "push", "-q", "origin", "feature/r:main")  # merged, branch kept
        sh(self.repo, "git", "checkout", "-q", "main")
        sh(self.repo, "git", "branch", "-qD", "feature/r")
        sh(self.repo, "git", "fetch", "-q")
        sh(self.repo, "git", "checkout", "-qb", "feature/r", "origin/feature/r")  # "second machine"
        self.assertTrue(self.blocked("git commit -m more"))

    def test_untracked_state_without_record_is_not_trusted(self):  # 7
        cli(self.repo, "new", "Rec")
        d = self.feature_dir()
        gd = self.repo / ".git"
        (gd / "sdlc-state-hashes.json").unlink()  # e.g. lost record
        r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})
        self.assertIn("changed outside sdlc.py", r.stderr)
        for f in ("sdlc-registry.json", "sdlc-state-hashes.json"):
            self.assertEqual(gate(self.repo, "Write", {"file_path": str(gd / f)}).returncode, 2, f)

    def test_missing_baseline_reports_nothing(self):  # 8
        (self.repo / "notes.txt").write_text("user's file\n")
        cli(self.repo, "new", "NoBase")
        for f in (self.repo / ".git").glob("sdlc-baseline-*.json"):
            f.unlink()
        out = subprocess.run([sys.executable, str(DRIFT)], env=ENV, capture_output=True, text=True,
                             input=json.dumps({"tool_name": "Bash", "cwd": str(self.repo)})).stdout
        self.assertEqual(out, "")

    def test_block_message_names_the_right_artifact(self):  # 14
        cli(self.repo, "new", "Full", "--track", "full")
        r = gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")})
        self.assertIn("spec.md", r.stderr)

    def test_router_matches_everyday_requests(self):  # eval finding
        for p in ("Add a `divide(a, b)` function to calc.py", "Fix the typo in the subtract docstring",
                  "Plan (do not implement yet) adding a power function", "please rename foo to bar",
                  "can you update the README badge"):
            self.assertIn("sdlc skill", route(self.repo, p).stdout, p)
        for p in ("what does this function do?", "thanks", "explain the gate", "List the developer settings"):
            self.assertEqual(route(self.repo, p).stdout, "", p)


class TestConfirmationReview(Repo):
    """Second (last) fix round."""

    def test_feature_branch_moved_onto_code_from_detached_commit(self):  # 1
        env = claude_env()
        cli(self.repo, "new", "Det2")
        sh(self.repo, "git", "add", "-A")
        run(self.repo, "git", "commit", "-qm", "docs", env=env)
        sh(self.repo, "git", "checkout", "-q", "--detach")
        (self.repo / "app.py").write_text("x = 8\n")
        sh(self.repo, "git", "commit", "-qam", "code while detached")  # plain git, as if hooks were skipped
        r = run(self.repo, "git", "branch", "-f", "feature/det2", "HEAD", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no approved plan", r.stderr)

    def test_renamed_or_branched_feature_keeps_its_gate(self):  # 2
        cli(self.repo, "new", "Keep")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "docs")
        sh(self.repo, "git", "checkout", "-qb", "wip")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)
        sh(self.repo, "git", "checkout", "-q", "feature/keep")
        sh(self.repo, "git", "branch", "-qD", "wip")
        sh(self.repo, "git", "branch", "-m", "feature/keep", "feature/renamed")
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.repo / "app.py")}).returncode, 2)

    def test_reopen_while_unapproved_keeps_drift(self):  # 3
        cli(self.repo, "new", "KeepDrift", "--track", "full")
        (self.repo / "app.py").write_text("x = 'hacked'\n")
        m = load_module(CLI, "sdlc_kd")
        self.assertIn("app.py", m.drift(self.repo, m.active_feature(self.repo)))
        cli(self.repo, "reopen", "spec")
        self.assertIn("app.py", m.drift(self.repo, m.active_feature(self.repo)))

    def test_develop_base_fresh_branch_is_not_stale(self):  # 4
        sh(self.repo, "git", "checkout", "-qb", "develop")
        sh(self.repo, "git", "push", "-qu", "origin", "develop")
        sh(self.repo, "git", "remote", "set-head", "origin", "develop")
        sh(self.repo, "git", "checkout", "-qb", "feature/d", "origin/develop")
        self.assertFalse(gate(self.repo, "Bash", {"command": "git commit -m x"}).returncode == 2)

    def test_forged_state_cannot_be_committed(self):  # 5
        env = claude_env()
        cli(self.repo, "new", "Forge2")
        state = self.feature_dir() / "state.json"
        state.write_text(state.read_text().replace('"approvals": {}', '"approvals": {"plan": {"sha256": "x"}}'))
        run(self.repo, "git", "add", "-A", env=env)
        r = run(self.repo, "git", "commit", "-qm", "docs", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("edited outside sdlc.py", r.stderr)

    def test_syncing_from_base_is_allowed_before_approval(self):  # 9
        env = claude_env()
        cli(self.repo, "new", "Sync")
        sh(self.repo, "git", "add", "-A")
        run(self.repo, "git", "commit", "-qm", "docs", env=env)
        other = Path(self.tmp.name) / "upstream-sync"
        sh(Path(self.tmp.name), "git", "clone", "-q", str(self.remote), str(other))
        (other / "base.py").write_text("b = 1\n")
        sh(other, "git", "add", ".")
        sh(other, "git", "commit", "-qm", "base moved")
        sh(other, "git", "push", "-q", "origin", "main")
        sh(self.repo, "git", "fetch", "-q")
        r = run(self.repo, "git", "merge", "-q", "--no-edit", "origin/main", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_verify_in_a_message_is_data(self):  # 10
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.assertFalse(gate(self.repo, "Bash", {"command": 'git commit -m "gate: refuse --no-verify"'}).returncode == 2)
        self.assertTrue(gate(self.repo, "Bash", {"command": "git commit --no-verify -m x"}).returncode == 2)


class TestBranchGate(Repo):
    def commit(self, cmd="git commit -m wip"):
        return gate(self.repo, "Bash", {"command": cmd})

    def test_blocks_commit_on_main(self):
        r = self.commit()
        self.assertEqual(r.returncode, 2)
        self.assertIn("on 'main'", r.stderr)

    def test_allows_fresh_branch_and_unrelated_commands(self):
        self.assertEqual(gate(self.repo, "Bash", {"command": "ls -la"}).returncode, 0)
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        self.assertEqual(self.commit().returncode, 0)

    def test_blocks_branch_whose_work_already_merged(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        (self.repo / "app.py").write_text("x = 2\n")
        sh(self.repo, "git", "commit", "-qam", "change")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/x")
        self.assertEqual(self.commit().returncode, 0)
        # merge on the "server": fast-forward main to the branch
        sh(self.repo, "git", "push", "-q", "origin", "feature/x:main")
        sh(self.repo, "git", "fetch", "-q")
        r = self.commit()
        self.assertEqual(r.returncode, 2)
        self.assertIn("merged", r.stderr)

    def test_blocks_branch_with_deleted_upstream(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/y")
        (self.repo / "app.py").write_text("x = 3\n")
        sh(self.repo, "git", "commit", "-qam", "change")
        sh(self.repo, "git", "push", "-qu", "origin", "feature/y")
        sh(self.repo, "git", "push", "-q", "origin", "--delete", "feature/y")
        sh(self.repo, "git", "fetch", "-q", "--prune")
        r = self.commit()
        self.assertEqual(r.returncode, 2)
        self.assertIn("deleted", r.stderr)

    def test_bypass(self):
        r = gate(self.repo, "Bash", {"command": "git commit -m x"}, env={**ENV, "SDLC_GATES": "off"})
        self.assertEqual(r.returncode, 0)


class TestPlanGate(Repo):
    def edit(self, rel):
        return gate(self.repo, "Edit", {"file_path": str(self.repo / rel)})

    def test_code_edits_wait_for_plan_approval(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/plain")
        self.assertEqual(self.edit("app.py").returncode, 0)  # no SDLC feature: allowed

        sh(self.repo, "git", "checkout", "-q", "main")
        cli(self.repo, "new", "Gated", "--track", "full")
        d = self.status()["dir"]
        r = self.edit("app.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("spec phase", r.stderr)
        self.assertEqual(self.edit(f"{d}/spec.md").returncode, 0)  # artifacts always editable
        self.assertEqual(self.edit("src/new_module.py").returncode, 2)  # new files too

        (self.repo / d / "spec.md").write_text(GOOD_SPEC)
        cli(self.repo, "approve", "spec", "--note", "user: yes")
        (self.repo / d / "plan.md").write_text(GOOD_PLAN)
        self.assertEqual(self.edit("app.py").returncode, 2)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        self.assertEqual(self.edit("app.py").returncode, 0)

        (self.repo / d / "plan.md").write_text(GOOD_PLAN.replace("do it", "do more"))
        r = self.edit("app.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("re-approve", r.stderr)

    def test_files_outside_repo_are_never_gated(self):
        cli(self.repo, "new", "Gated")
        r = gate(self.repo, "Write", {"file_path": str(Path(self.tmp.name) / "notes.md")})
        self.assertEqual(r.returncode, 0)

    def test_garbage_input_allows(self):
        r = subprocess.run([sys.executable, str(GATE)], input="not json", env=ENV,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
