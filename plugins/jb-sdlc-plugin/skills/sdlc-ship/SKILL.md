---
name: sdlc-ship
description: SDLC phase 5 — ships a reviewed feature, covering the fresh-branch check, version bump, final commit including docs/sdlc artifacts, push, and a PR whose body links the plan, review and (on the full track) spec. Use when the active SDLC feature is in the ship phase, or the user asks to ship, open the PR, or release the feature.
effort: low
---

# Phase 5 — Ship

## Steps

1. **Branch freshness**, which the user's global rule requires before every commit:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" check-branch
   ```
   If it prints `STALE`, stop. The work must move onto a fresh branch off the
   updated base. Explain that to the user and ask before rebasing or
   cherry-picking.
2. **Version bump**, only where the repo versions releases:
   - Claude Code plugin: `.claude-plugin/plugin.json` `version`. Use a minor
     bump for a new skill, agent or feature and a patch bump for a fix.
   - Python: `pyproject.toml` `version`, then `uv lock`.
   - Android: `versionCode` +1 and `versionName` in `app/build.gradle(.kts)`.
3. **Re-run the verification commands** if anything changed since the build
   evidence was recorded, version bumps included.
4. **Commit** the remaining changes. Include the `docs/sdlc/<feature>/` folder
   and end the message with the session's commit attribution lines.
5. **Confirm, then publish.** Pushing and opening a PR are outward-facing, so
   ask once ("Push `<branch>` and open a PR against `<base>`?") unless the user
   already said to ship. Then:
   ```bash
   git push -u origin HEAD
   gh pr create --base <base> --title "<title>" --body-file <tmp body file>
   ```
   PR body: a 2–4 line summary, a list of the ACs, links to the spec, plan
   and review files in the branch, the build evidence line, and the session's
   PR attribution line. Leave out real financial figures.
6. **Close the gate:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve ship --pr <url>
   ```
   Then commit and push that state.json change. This keeps the merged record
   showing `done`.
7. Report the PR URL. Remind the user of two things:
   - After they merge it, the next feature starts from fresh `main`
     (`git checkout main && git pull`), and the hook blocks further commits
     on this branch.
   - Run `/clear` before the next task. State is on disk, so nothing is
     lost, and a fresh context costs far less per turn than this one.
