# jb-sdlc-plugin

A lean, gated, cost-aware AI SDLC for Claude Code that works in any git repo.
Unlike the other plugins in this marketplace, it doesn't depend on
`jb_gateway_mcp`.

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontFamily": "ui-sans-serif, system-ui", "lineColor": "#64748B"}}}%%
flowchart LR
    P(["implement X / fix bug / take task"]):::actor --> R["router hook"]:::changed
    R --> Z{"size it"}:::risk
    Z -- quick --> Q["just do it"]:::external
    Z -- lite --> PL["plan + ACs"]:::new
    Z -- full --> SP["spec"]:::new --> PL
    PL -- "you approve" --> B["build on Sonnet<br/>parallel lanes"]:::new
    B -- "tests green" --> RV["review<br/>Verdict: PASS"]:::test
    RV --> SH["ship: PR"]:::new
    classDef new fill:#DCFCE7,stroke:#16A34A,color:#14532D,stroke-width:2px
    classDef changed fill:#DBEAFE,stroke:#2563EB,color:#1E3A8A,stroke-width:2px
    classDef external fill:#F3F4F6,stroke:#6B7280,color:#111827,stroke-dasharray:4 3
    classDef risk fill:#FEF3C7,stroke:#D97706,color:#78350F,stroke-width:2px
    classDef test fill:#F3E8FF,stroke:#9333EA,color:#581C87
    classDef actor fill:#FFE4E6,stroke:#E11D48,color:#881337,stroke-width:2px
```

## Components

| Component | Model / effort | Role |
|---|---|---|
| `sdlc` skill | session model | Orchestrator. Sizes the task (quick / lite / full), routes to the next phase and applies the cost rules. |
| `sdlc-spec` skill | session, `medium` | Writes the one-page spec with testable ACs (full track only). |
| `sdlc-plan` skill | session, **`high`** | Writes the plan: files, steps, AC→test map, verification commands. |
| `sdlc-build` skill | thin coordinator | Delegates to `sdlc-builder`, handles BLOCKED decisions with you, spot-checks the result and records evidence. |
| `sdlc-review` skill | thin coordinator | Runs `sdlc-reviewer` on every track, plus `code-review` (high) on the full track and `security-review` when relevant. |
| `sdlc-ship` skill | session, `low` | Branch check, version bump, PR, and a `/clear` reminder. |
| `sdlc-scout` agent | **haiku**, `low`, 30 turns, no CLAUDE.md | Read-only code locator, used instead of the built-in Explore (which runs on Opus). |
| `sdlc-builder` agent | **sonnet**, `medium` (`high` on full track), 120 turns | Test-first implementation in an isolated context. |
| `sdlc-reviewer` agent | **sonnet**, `high`, 40 turns | Fresh-context review against the ACs and plan, tests and private-data leaks. |
| `hooks/route.py` | UserPromptSubmit | Adds one context line pointing development prompts at `sdlc`, and announces the active feature and phase. |
| `hooks/lean.py` | PostToolUse | Flags output-token waste as it's written: comment-heavy edits, comments that narrate code, commented-out code, and whole-file Writes that changed only a little. It sends a one-line hint back and never blocks. |
| `hooks/gate.py` | PreToolUse | Blocks commits on main or merged branches, pushes to main, and code edits before the plan is approved. |
| `scripts/sdlc.py` | stdlib CLI | `new`, `track`, `status`, `approve`, `reopen`, `check-diagrams`, `check-branch`. |
| `references/diagrams.md` | — | Mermaid house style (muted, semantic palette that works in light and dark themes) and templates: user flow, change map, sequence, lanes, state, ER. |

## Cost model

These are API list prices per million tokens (input / output), from the
`claude-api` reference as of 2026-11-03:

| Model | $ in / out | Used for |
|---|---|---|
| Opus (`opus`, Opus 5.5) | 4 / 20 | Your session: sizing, spec, plan, coordination |
| Sonnet (`sonnet`, Sonnet 5.5) | 2 / 10 | Build (most tokens) and review |
| Haiku (`haiku`, Haiku 5.5) | 0.10 / 0.50 | Code search for spec and plan |

The levers below are listed by impact. They follow Anthropic's Claude Code
cost guidance: Sonnet for most coding, Opus for architecture and reasoning,
Haiku for simple subagents, verbose work kept in subagents, and small
contexts.

1. **Right-size the ceremony.** Most work is quick or lite. Full-track
   overhead (separate spec, extra review passes) only applies where an error
   is expensive: money math, auth, migrations, packaging.
2. **Put bulk tokens on Sonnet.** The build phase, with its file reads,
   edits and test output, runs in `sdlc-builder` at half Opus's per-token
   price, in a fresh context. Only a short status block returns to the main
   session.
3. **Search on Haiku.** At these prices, `sdlc-scout` reads code at about
   1/40 of Opus's input cost and skips loading CLAUDE.md.
4. **Spend thinking on judgment, not typing.** The plan runs at `high`
   effort, while build and ship run at `medium` and `low`. Nothing uses
   `max` unless you ask.
5. **Keep the cache warm.** Each phase stays on one model; switching
   models drops the prompt cache, since it is per model. Separate models
   live in separate subagent contexts.
6. **Make resuming cheap.** State lives on disk and the router re-announces
   the phase, so `/clear` between features costs nothing.
7. **Don't generate what nobody reads.** Output tokens cost 5× input. Code comments carry only the non-obvious why, with no narration, banners or commented-out code. Edits are minimal diffs, not file rewrites. Chat replies don't echo code or tool output. Agents return fixed blocks. `lean.py` and the reviewer both check for this.
8. **Cap runaway work.** `maxTurns` limits every agent, and review fixes
   re-run only the reviewer, not the whole review stack.

## Parallelism

Parallel subagents cut wall-clock time, not tokens, so the kit fans out only
where jobs are independent:

| Phase | Runs concurrently |
|---|---|
| spec / plan | one `sdlc-scout` per code area |
| build | one `sdlc-builder` per **plan lane**, each in its own git worktree (`isolation: worktree`), merged in lane order, then one integration test run |
| review | `sdlc-reviewer` alongside `code-review` / `security-review` |
| several tasks | one worktree + feature branch + builder per task, with approvals batched into one question |

Lanes need disjoint file sets, and at most 3 run at once. A merge conflict
falls back to a sequential rebuild of that lane, never a hand-merge.
Sequential is the default.

To measure, `/usage` in Claude Code attributes your plan usage to skills,
subagents and plugins. The session model stays whatever
`~/.claude/settings.json` sets. If you'd rather Claude Code switch models
itself, `opusplan` (Opus in plan mode, Sonnet otherwise) is the built-in
equivalent. This plugin already routes per phase, so you don't need it.

## Artifacts

The full-track `spec.md` carries a user-flow diagram. `plan.md` carries a
change map on the full track, or when it touches 3 or more files or has
lanes, plus a sequence or ER diagram where relevant. `approve` refuses a
diagram that fails the lint. That lint is a built-in syntax check, plus a
full render with `mmdc` if it is installed.

```
docs/sdlc/2026-11-20-card-import/
  spec.md      full track only: problem, ACs, non-goals
  plan.md      (lite: goal + ACs +) files, steps, AC→test map, verification
  review.md    AC table, findings, Verdict
  state.json   track, branch, base, approvals (managed by sdlc.py)
```

The phase is derived from the files, not stored. Approvals of
spec, plan and review are tied to the file's sha256, so editing an approved
file re-opens its gate and every gate after it.

## Gates and switches

| Gate | Applies | Blocks |
|---|---|---|
| Fresh branch | every repo, every `git commit`/`git push` | Commits on `main`/`master` and pushes to them. Also branches whose upstream is `[gone]`, whose pushed commits are all in base, or whose PR is merged (checked with `gh`). |
| Plan approved | branches with an active `docs/sdlc` feature | Edit/Write outside `docs/sdlc/` until the plan (and the spec, on the full track) is approved |

Three switches control the hooks:

- `SDLC_GATES=off` disables both gates.
- `SDLC_AUTOROUTE=off` disables the router.
- `SDLC_NO_GH=1` skips the merged-PR lookup.
- `SDLC_LEAN=off` disables the output-token watchdog.

A hook that hits an internal error allows the action.

The gate trusts Claude to approve the spec and plan only after you
explicitly say yes. The skills require this, but a hook can't tell who
typed the approval.

## Install

```
/plugin install jb-sdlc-plugin@jb-claude-plugins
/reload-plugins
```

Requires `python3` (stdlib only) and `git`; `gh` is recommended.

## Tests

```bash
python3 -m unittest discover -s plugins/jb-sdlc-plugin/tests -v
```
