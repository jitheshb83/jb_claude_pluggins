---
name: sdlc-ship
description: SDLC phase 5. Prepares a reviewed feature for the user to publish, covering the fresh-branch check, version bump, a final local commit including the docs/sdlc artifacts, and a pr.md PR description. It hands over the exact push and PR commands and never pushes, opens a PR or merges itself. Use when the active SDLC feature is in the ship phase, or the user asks to ship, wrap up or prepare the PR.
effort: low
---

# Phase 5: Ship (a local hand-off, never a publish)

The SDLC never runs `git push`, `gh pr create` or `gh pr merge`, and never
merges into the base branch. The hook blocks all of these. Publishing is
the user's step.

## Steps

1. **Check the branch is fresh.** The user's global rule requires this
   before every commit:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" check-branch
   ```
   If it prints `STALE`, stop. Explain the situation and ask how to move the
   work onto a fresh branch.
2. **Bump the version,** only where the repo versions its releases:
   - Claude Code plugin: `version` in `.claude-plugin/plugin.json`. Use a
     minor bump for a new skill, agent or feature, a patch bump for a fix.
   - Python: `version` in `pyproject.toml`, then `uv lock`.
   - Android: `versionCode` +1 and `versionName` in `app/build.gradle(.kts)`.
3. **Re-run the verification** if anything changed since the build evidence
   was recorded, including the version bump.
4. **Write `<feature dir>/pr.md`,** the PR description:
   - a 2–4 line summary;
   - the acceptance criteria;
   - links to the plan, the review and (full track) the spec;
   - the build evidence line;
   - the session's PR attribution line.

   Leave out real financial figures.
5. **Close the gate,** then **commit everything locally**, `docs/sdlc/<feature>/`
   included, ending the message with the session's attribution lines:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve ship
   git add -A && git commit -m "<summary>"
   ```
6. **Hand over** in five lines or fewer: the branch, the commit count, the
   verification status, and the commands for the user to run:
   ```bash
   git push -u origin <branch>
   gh pr create --base <base> --title "<title>" --body-file docs/sdlc/<feature>/pr.md
   ```
   Then remind them of two things:
   - After the PR merges, run `git checkout main && git pull`. The hook
     blocks further commits on this branch.
   - Run `/clear` before the next task. State lives on disk.
