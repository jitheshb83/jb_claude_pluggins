"""End-to-end tests for sdlc.py and the PreToolUse gate, against throwaway git repos."""
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
ENV.pop("SDLC_GATES", None)
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
        state = self.feature_dir() / "state.json"
        r = gate(self.repo, "Edit", {"file_path": str(state)})
        self.assertEqual(r.returncode, 2)
        self.assertIn("only by `sdlc.py approve`", r.stderr)
        r = gate(self.repo, "Bash", {"command": f"sed -i '' s/a/b/ {state}"})
        self.assertEqual(r.returncode, 2)
        self.assertEqual(gate(self.repo, "Edit", {"file_path": str(self.feature_dir() / "plan.md")}).returncode, 0)

    def test_shell_writes_obey_the_plan_gate(self):  # finding 4
        cli(self.repo, "new", "Shell writes")
        d = self.status()["dir"]
        for cmd in ("sed -i '' 's/1/2/' app.py", "cat > src/new.py <<'EOF'\nx = 1 > 0\nEOF",
                    "echo x >> app.py", "printf y | tee app.py", "cp /tmp/x app.py",
                    "rm app.py", "git apply fix.patch", "touch src/z.py"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 2, cmd)
        for cmd in (f"cat > {d}/plan.md <<'EOF'\n# a > b\nEOF", "ls > /dev/null 2>&1",
                    "uv run pytest -q 2>&1 | tail -n 5", "grep -n x app.py"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}).returncode, 0, cmd)
        (self.repo / d / "plan.md").write_text(GOOD_PLAN_NODIAG)
        cli(self.repo, "approve", "plan", "--note", "user: yes")
        self.assertEqual(gate(self.repo, "Bash", {"command": "sed -i '' 's/1/2/' app.py"}).returncode, 0)

    def test_subshells_wrappers_and_bash_c_are_followed(self):  # finding 5
        sh(self.repo, "git", "checkout", "-q", "main")
        elsewhere = Path(self.tmp.name)
        for cmd in (f"(cd {self.repo} && git commit -m x)", f"sudo git -C {self.repo} commit -m x",
                    f"env FOO=1 git -C {self.repo} commit -m x",
                    f"bash -c 'cd {self.repo} && git commit -m x'",
                    f"echo $(git -C {self.repo} commit -m x)"):
            self.assertEqual(gate(elsewhere, "Bash", {"command": cmd}).returncode, 2, cmd)

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
        for cmd in ("git push -u origin HEAD", "gh pr create --fill", "gh pr merge 6 --squash",
                    "cd . && git push"):
            r = gate(self.repo, "Bash", {"command": cmd})
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("never", r.stderr)
        sh(self.repo, "git", "checkout", "-q", "main")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git merge feature/x"}).returncode, 2)
        sh(self.repo, "git", "checkout", "-q", "feature/x")
        self.assertEqual(gate(self.repo, "Bash", {"command": "git merge --no-ff lane-a"}).returncode, 0)

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

    def test_follows_cd_and_dash_C(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        outside = self.repo.parent
        sh(self.repo, "git", "checkout", "-q", "main")
        self.assertEqual(gate(outside, "Bash", {"command": f"cd {self.repo} && git commit -m x"}).returncode, 2)
        self.assertEqual(gate(outside, "Bash", {"command": f"git -C {self.repo} commit -m x"}).returncode, 2)
        self.assertEqual(gate(outside, "Bash", {"command": "git commit -m x"}).returncode, 0)  # not a repo

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

    def test_blocks_push_to_main_from_feature_branch(self):
        sh(self.repo, "git", "checkout", "-qb", "feature/x")
        allow = {**ENV, "SDLC_ALLOW_PUSH": "1"}
        for cmd in ("git push origin HEAD:main", "git push origin +refs/heads/main", "git push origin +main"):
            self.assertEqual(gate(self.repo, "Bash", {"command": cmd}, env=allow).returncode, 2, cmd)
        self.assertEqual(gate(self.repo, "Bash", {"command": "git push -u origin HEAD"}, env=allow).returncode, 0)

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
