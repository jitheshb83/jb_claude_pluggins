---
name: sdlc-build
description: SDLC phase 3. Delegates the approved plan.md to sdlc-builder agents (Sonnet, test-first, isolated contexts). It runs independent plan lanes in parallel, each in its own git worktree, then merges them and runs the integration suite. It handles BLOCKED decisions with the user and records passing-test evidence to close the build gate. Use when the active SDLC feature is in the build phase, or the user asks to implement, build or code the planned feature.
---

# Phase 3: Build

Building is where most tokens go, so it runs on Sonnet, which costs half as
much as Opus per token, inside builder subagents. The main session only
coordinates. If plan.md defines parallel lanes, the builders run at the same
time.

## 0. Pick the mode from plan.md

| Plan shape | Mode |
|---|---|
| 1–2 steps touching 1–2 files | **Inline.** Build it yourself, test-first. A subagent's startup cost isn't repaid here. |
| One lane, the normal case | **Single builder.** One `sdlc-builder` on the feature branch. |
| Lane 0 and/or lanes A–C | **Parallel lanes.** See section 2. |

For every builder call, pass:
- the feature directory and track
- the session's commit attribution lines
- `effort: "high"` on the full track, or when the lane touches money math or concurrency

Otherwise the builder uses its default effort, medium.

## 1. Single builder

Spawn `sdlc-builder` on the feature branch with no isolation, then go to
section 3.

## 2. Parallel lanes

1. **Commit the docs first.** Worktrees branch from `HEAD`, so commit
   `docs/sdlc/<feature>/` (the approved plan and state.json) before any
   fan-out. Otherwise the builders can't see the plan.
2. **Lane 0 first, alone.** Run one builder on the feature branch for the
   shared foundation, with no isolation, and wait for DONE.
3. **Fan out.** Spawn one `sdlc-builder` per lane **in a single message**
   with `isolation: "worktree"`, so they run concurrently. Give each builder:
   - its lane letter and the lane's step numbers and file list
   - the instruction: "lane mode: touch only these files"
4. **Merge in lane order** on the feature branch, once all lanes report back:
   ```bash
   git merge --no-ff --no-edit <lane-branch>   # branch name from the builder's report
   ```
   If a merge conflicts, run `git merge --abort`. Then re-run that lane
   **sequentially** with one builder on the feature branch, using the lane's
   steps and telling it to apply them on top of the current code. Never
   hand-resolve conflicts in the main session.
5. **Integration check.** Run the plan's full verification once, on the
   feature branch, with quiet output. The lanes were only tested alone.
6. **Clean up.** For each merged lane, run
   `git worktree remove <path> && git branch -d <lane-branch>`. `-d` refuses
   to delete an unmerged branch, which keeps this safe.

## 3. Results, spot-check and gate

- **BLOCKED:** put the decision to the user in one `AskUserQuestion`. If
  several lanes are blocked, batch their questions into one call (up to 4).
  If an answer changes the plan, edit `plan.md` and get the user's
  re-approval (`approve plan --note "re-approved: <change>"`). Then re-spawn
  only the affected builder(s). Never patch the code yourself in the main
  session.
- **Spot-check (cheap).** Re-run the main test command once
  (`-q … | tail -n 15`) and run `git log --oneline <base>..HEAD`. If either
  disagrees with the builders' reports, send the work back.
- **Close the gate:**
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve build \
    --evidence "uv run pytest -q: 58 passed (lanes A,B merged); QT offscreen smoke: exit 0"
  ```
  Report anything unverifiable here (a device, live bank consent,
  notarization credentials) in `--evidence` and in your summary. Never
  present it as verified.
- Continue with `sdlc-review`.
