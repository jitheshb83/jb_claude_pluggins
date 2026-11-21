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

## Read-only planning (native plan mode)

Planning runs read-only. Writing code before approval is what the gates
exist to prevent, so use Claude Code's own mechanism first:

1. Run `sdlc.py new` (or `status`) **before** entering plan mode. Plan
   mode refuses writes.
2. If the `EnterPlanMode` tool is available (interactive sessions), call
   it. Scouts (read-only), file reads and `AskUserQuestion` all work inside
   plan mode.
3. Present the finished artifact with `ExitPlanMode`. The user's approval
   there is the approval. Then write the file from exactly that text and
   record it with `approve plan --note "approved in plan mode: <their words>"`.
4. If `EnterPlanMode` isn't available (headless or `-p` runs), use the
   `AskUserQuestion` approval described below.

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
   Treat the scout's map as leads, not evidence. Before citing anything as
   fact, open the cited lines yourself.
3. **Discuss the approach before writing the plan.** Present:
   - the evidence: what exists and what will change, each with `path:line`;
   - the proposed approach and the one realistic alternative, with the
     trade-off between them;
   - the questions that decide the plan.

   Ask in one `AskUserQuestion` and wait for the answers. Never plan around
   something you assumed: an unverified API, file, flag, behaviour or
   number is a question, not a step.
4. **Write plan.md** from the template that fits the track. Steps must be
   small, ordered, and able to keep the suite green between them. Then
   decide the **lanes** (see below).
   - **Diagrams**, following the house style in `${CLAUDE_PLUGIN_ROOT}/references/diagrams.md`:
     - a **change map**, required on the full track or when the plan touches
       3 or more files: new, changed and external nodes, with test links;
     - a **sequence diagram** if the change crosses a process, API, MCP or
       hook boundary;
     - an **ER diagram** for schema changes;
     - a **lanes** diagram if the plan has lanes.

     Use at most 2 diagrams. Run `sdlc.py check-diagrams`; `approve plan`
     refuses if a diagram fails the lint or a required one is missing.
5. **Approval, always before any implementation.** Show the plan (on the
   lite track: the Goal, the Acceptance criteria, the Changes table and the
   diagram link) and ask whether to approve or change it. `approve plan`
   refuses when a section is empty, when Evidence cites nothing actually
   read or run, when an open question remains, when a diagram is broken or
   missing, or when `--note` is missing. Once the user approves:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve plan --note "<user's words>"
   ```
   Continue with `sdlc-build`.

## Template (lite adds Goal and Acceptance criteria at the top)

~~~markdown
# Plan: <title>

## Goal                      <!-- lite only: 1–2 sentences -->
## Acceptance criteria       <!-- lite only: AC1: Given/when/then ... -->

## Evidence
- <path:line or `command`>: <what it shows>. Write 'nothing existing' for code that is entirely new.

## Approach
<One paragraph, plus the rejected alternative in one line.>

```mermaid
<change map: flowchart TD, a subgraph per area, nodes classed new/changed/external/risk/test>
```

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

## Decisions
- <question> → <user's answer>

## Open questions
None
~~~

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
