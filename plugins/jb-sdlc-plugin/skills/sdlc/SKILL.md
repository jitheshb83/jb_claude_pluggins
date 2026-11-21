---
name: sdlc
description: The user's default workflow for ALL development work. It sizes each task as quick, lite or full, runs it through Spec → Plan → Build (test-first) → Review → Ship with committed artifacts and hook-enforced gates, and routes each phase to the cheapest model that does it well. Use whenever the user asks to develop, implement, build, add, fix, refactor or migrate something, or to take, pick up or work on a task, ticket or issue. Also use for /sdlc, for "what phase is this in", or when resuming SDLC work on a branch.
when_to_use: "implement X", "build a script that…", "add a feature/option/command", "fix this bug", "take this task", "work on issue #12", "refactor Y", "ship it", "continue the feature". Not for pure questions or explanations, and not for one-off throwaway scripts outside a repo.
argument-hint: "[what to build | status]"
---

# AI SDLC orchestrator

The workflow has five gated phases, one skill each. This skill decides the
**track** and the **next phase** and keeps cost proportional to risk.

| Phase | Skill | Runs on | Produces | Gate |
|---|---|---|---|---|
| spec (full only) | `sdlc-spec` | session model; `sdlc-scout` on Haiku to locate code | `spec.md` | **User** approves |
| plan | `sdlc-plan` | session model at `effort: high`; `sdlc-scout` on Haiku | `plan.md` | **User** approves |
| build | `sdlc-build` | `sdlc-builder` on **Sonnet**, isolated context | code, tests, commits | Tests green, evidence recorded |
| review | `sdlc-review` | `sdlc-reviewer` on Sonnet at `effort: high`, plus `code-review` on the full track | `review.md` | `Verdict: PASS` |
| ship | `sdlc-ship` | session model at `effort: low` | local commit + `pr.md` | **User** pushes and opens the PR; never the SDLC |

## 1. Size the task first: this is the biggest cost lever

| Track | Use when | Ceremony |
|---|---|---|
| **quick** | One file, an obvious change and no behaviour ambiguity: a typo, a config value, a rename, a dependency bump. | No SDLC files, but **still a plan and an approval**. State the change in 2–4 lines (file, edit, how it will be verified, with evidence for anything claimed about current code) and get an explicit yes. Then do it, run the tests, and commit locally on a fresh branch. |
| **lite** (default) | A clear request: about 5 files or fewer, no new dependency, no schema or data migration, no auth, token or money-math change. | One `plan.md` holding the acceptance criteria, one user approval, a Sonnet build and a single reviewer pass. |
| **full** | Any one of: ambiguous requirements, a new module or subsystem, a schema or data migration, auth/tokens/MCP tool exposure, **money, loan or amortization calculations**, packaging/codesigning, cross-repo work, or more than about 5 files. | Separate spec and plan approvals, a reviewer pass plus `code-review` (high), and `security-review` when relevant. |

State the track and one line of reasoning in one sentence. If the user
disagrees, switch. If a lite task grows mid-flight, escalate with
`sdlc.py track full`, which adds a spec and re-opens the gates.

## 2. Route

```bash
SDLC="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py"
$SDLC status                            # active feature for this branch?
$SDLC new "<short title>" --track lite  # or --track full; branches off main for you
```

- **No active feature:** size the task. For quick, follow the quick row of
  the sizing table (chat plan, then yes, then do it). For lite or full, run
  `new` (it pulls the base and branches) and continue with `sdlc-plan` (lite)
  or `sdlc-spec` (full).
- **Active feature:** invoke the skill for its `phase`. If a gate shows
  `stale`, the artifact changed after approval, so return to that phase.
- **`done`:** handed off. The user pushes and opens the PR; the next feature starts from a fresh `main`.

State lives on disk (`docs/sdlc/<date>-<slug>/state.json`), so after `/clear`
the router hook re-announces the active phase. Resuming is cheap, and the
user should `/clear` between features rather than carry a long context.

## 3. Plan by discussion and evidence, never by imagination

These rules hold for every track, quick included:

1. **Evidence first.** Every statement about existing code, config,
   behaviour or tooling must come from something actually read or run in
   this session, cited as `path:line` or as a `command` with its result. If
   you haven't verified something, it's an open question, not a fact. Never
   invent a file, function, flag, API or number.
2. **Discuss before you write the plan.** Present what you found, with its
   evidence, then the approach with the one realistic alternative, then the
   questions only the user can answer. Use one `AskUserQuestion` round. Wait
   for the answers.
3. **Record the outcome.** Answers go under `## Decisions` in the artifact,
   as "question → user's answer". Anything still unclear stays under
   `## Open questions`, and `approve` refuses while that section isn't
   `None`.
4. **Ask before every implementation.** Nothing is implemented without the
   user's explicit approval of the plan as written. That includes quick
   tasks, re-plans after BLOCKED, and the fix list from review. `approve`
   requires `--note` with the user's words.
5. **Publishing is the user's call.** The SDLC commits locally and never
   pushes or opens a PR on its own: a plain feature-branch `git push` or
   `gh pr create` goes through the permission prompt, which the user confirms.
   It never merges a PR or pushes to or merges into the base branch. The hook enforces this.

## 4. Cost rules (all phases)

- **One model per phase, no flip-flopping.** The prompt cache is per
  model, so every switch re-reads the whole context uncached. Heavy phases
  run in their own subagents, which have their own small, cached contexts.
- **Locate code with `sdlc-scout`** (Haiku), never the built-in Explore,
  which runs on Opus. Read only the files the scout points at.
- **Keep bulky output out of the main context.** Tests run inside
  `sdlc-builder` and come back as a summary. In the main session, use quiet
  flags and `tail`.
- **Keep artifacts short.** They are re-read in later phases: a spec is
  about one page and a plan fits on one screen.
- **Spend on judgment.** Plan at high effort and review at high effort.
  Typing out code goes on Sonnet at medium. Use `max` only when the user
  asks.

## 5. Output discipline: don't pay for tokens nobody needs

Output tokens cost 5× input on every model, and narration is pure output.
These rules apply to you and to every subagent you brief:

- **Code comments: the non-obvious *why* only.** That means a constraint, a
  workaround, a gotcha, or a link to an issue. No comments that restate the
  code (`# loop over accounts`), no banner or section comments, no
  commented-out code (git keeps history), and no docstrings on private or
  trivial functions unless the repo's convention requires them. Match the
  surrounding file's density.
- **Edit, don't rewrite.** Change existing files with Edit and a minimal
  diff. Use Write only for new files. Don't re-read a file after editing it.
- **Don't echo work back.** In chat, never paste code or diffs you just
  wrote, restate tool output, or narrate what you're about to do. Give the
  result, the file links and the verification status.
- **Keep reports short.** A phase summary is 5 lines or fewer. A final
  summary covers what changed, where, and whether it's verified, in about
  10 lines.
- **Artifacts are tables and bullets, not prose.** They get re-read in
  every later phase.
- **Subagent briefs and returns stay tight.** Give the task, the paths, and
  the return format. Agents return their fixed block, not essays.

The `lean.py` hook flags comment-heavy edits, narrating comments,
commented-out code and whole-file rewrites as they happen. Take the hint
for the next edits. Don't spend more tokens re-editing a file just to strip
a couple of comments.

## 6. Parallelism: finish faster where work is independent

Parallel subagents cut wall-clock time, but every extra agent pays its own
startup context. Fan out where the jobs are truly independent and big enough
to be worth it. Sequential stays the default.

| Where | What runs in parallel | Condition |
|---|---|---|
| spec / plan | One `sdlc-scout` per code area, spawned in one message | The feature spans separate areas (backend + app, plugin + MCP server) |
| build | One `sdlc-builder` per plan lane, each in its own worktree, then merged | The plan defines disjoint lanes; at most 3 |
| review | `sdlc-reviewer` alongside `code-review` / `security-review` | Always: they're independent |
| review fixes | One builder per lane of fixes | The fixes fall in disjoint files |

To make parallel spawns actually concurrent, issue them **in a single
message**. Batch the user's decisions from parallel work into one
`AskUserQuestion` (up to 4 questions), not one round-trip each.

### Several independent tasks at once

When the user hands over 2–3 unrelated tasks, give each its own feature
branch and worktree. Each task also gets its own PR.

1. For each task, run
   `git worktree add ../<repo>-<slug> -b feature/<slug> <base>`. Then run
   `sdlc.py new "<title>"` from inside that worktree; it adopts the existing
   branch.
2. Plan each task. Approvals still need the user, so batch them: present
   the plans together and ask in one `AskUserQuestion`, one question per
   task.
3. Spawn one `sdlc-builder` per task in a single message, each told to work
   only under its worktree's absolute path. Review the same way, then hand
   each one off separately. The user pushes each branch.

Don't run tasks in parallel if they touch the same files, or if one depends
on another. In those cases, chain them.

## 7. Rules that hold in every phase

- **Never approve a user gate yourself.** Run `approve spec` or `approve plan`
  only after the user says yes to that exact artifact. Ask with one
  `AskUserQuestion` and put their words in `--note`. You record build and
  review gates yourself from evidence.
- **Hooks are guardrails.** If one blocks you, fix the cause. Suggest
  `SDLC_GATES=off` only when the user asks to bypass. Never edit `state.json`;
  only `sdlc.py` writes it. Before the plan is approved, don't write code
  through the shell or an interpreter either (`python -c`, `node -e`), even
  though the hook can't see inside those.
- **Private data stays local.** Artifacts, fixtures and PR text never
  contain real balances, transactions, IBANs, employer or loan figures.
- Commit `docs/sdlc/...` with the code, so the PR carries its plan and review.
