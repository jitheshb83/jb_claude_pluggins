---
name: sdlc-plan
description: SDLC phase 2. Writes plan.md (approach, files, ordered steps, an acceptance-criterion-to-test map and verification commands; on the lite track it also holds the goal and acceptance criteria) and gets the user's approval, which unlocks code edits. Use when the active SDLC feature is in the plan phase, or the user asks to plan, re-plan or approve an implementation plan.
effort: high
---

# Phase 2: Plan

Goal: decide **how**, concretely enough that build is mechanical and can run
on Sonnet. The plan is where an error costs the most later, which is why
this phase runs at high effort. The hook blocks code edits outside
`docs/sdlc/` until the plan is approved.

## Steps

1. **Inputs.** On the full track, read the approved `spec.md`; if it's
   wrong, `sdlc.py reopen spec` and go back. On the lite track, the user's
   request is the spec.
2. **Map the code cheaply.** Spawn `sdlc-scout` (Haiku) unless the spec
   phase already did. If the feature spans separate areas, for example a
   Python backend and the Kotlin app, or a plugin and its MCP server, spawn
   **one scout per area in the same message** so they run in parallel.
   Then read in full only the files they name that you will change or
   depend on.
3. **Write plan.md** from the template that fits the track. Steps must be
   small, ordered, and able to keep the suite green between them. Then
   decide the **lanes** (see below).
4. **Approval.** Show the plan, or on the lite track its Goal and
   Acceptance criteria and the Changes table, and ask whether to approve or
   change it. Once the user approves:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve plan --note "<user's words>"
   ```
   Continue with `sdlc-build`.

## Template (lite adds Goal and Acceptance criteria at the top)

```markdown
# Plan: <title>

## Goal                      <!-- lite only: 1–2 sentences -->
## Acceptance criteria       <!-- lite only: AC1: Given/when/then ... -->

## Approach
<One paragraph.>

## Changes
| File | Change |
|---|---|

## Steps
Lane 0 (sequential, shared foundation): <omit if none>
1. <Small, verifiable step> (verify: <command>)
Lane A (files: <paths>):
2. ...
Lane B (files: <paths>):
3. ...

## Test plan
| AC | Test (file::name) | Kind |
|---|---|---|

## Verification
- `<full test command, quiet>`
- `<lint / type-check>`
- <smoke check>

## Risks & rollback     <!-- full track; lite: one line or omit -->
```

## Verification by stack

| Stack | Commands |
|---|---|
| Python / uv | `uv run pytest -q`, plus `uv run ruff check .` if configured |
| PySide6 apps | the above, plus a headless smoke: `QT_QPA_PLATFORM=offscreen uv run python -m <app>` (launch, exercise, exit 0) |
| Kotlin / Gradle (Android) | `./gradlew test lint -q`; add `connectedAndroidTest` only if a device is attached |
| Claude Code plugin | `claude plugin validate <dir>`, script unit tests, and a real skill or hook invocation |
| macOS `.app` | build, then `codesign --verify --deep --strict` and `spctl -a -vv` |

Every acceptance criterion maps to at least one test, or to an explicit
scripted smoke step. List nearby cleanups as follow-ups, not as plan steps.

## Lanes: planning for parallel builds

Lanes let `sdlc-build` run several builders at once, each in its own git
worktree. They save wall-clock time, not tokens: every extra builder pays its
own startup context, so add lanes only when they clearly pay off.

- **Lane 0** holds the shared foundation that later work builds on, such as
  a new model or interface, or a shared fixture. It runs first, alone.
- **Lanes A, B, C** may run in parallel only if all of these hold:
  - their file sets are **disjoint**, tests included;
  - none needs another lane's output;
  - each is substantial, with at least 2 steps or about 3 files.
- Use at most **3** parallel lanes. If the changes are small or tangled,
  use a single lane. That is the default, and it's always correct.
- Put each lane's file list in its heading. A builder that needs a file
  outside its lane stops and reports back rather than colliding with
  another lane.
