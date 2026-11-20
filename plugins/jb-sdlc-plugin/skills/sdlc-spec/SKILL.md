---
name: sdlc-spec
description: SDLC phase 1 (full track only). Turns a feature request into a one-page spec.md with testable acceptance criteria and gets the user's approval. Use when the active SDLC feature is on the full track in the spec phase, or the user asks to write, revise or approve a spec or requirements.
effort: medium
---

# Phase 1: Spec (full track)

Goal: agree on **what** and **why** before anyone decides how. The output is
`<feature dir>/spec.md`, at the path `sdlc.py status` gives. Lite-track
features skip this phase, because their acceptance criteria live in
`plan.md`.

## Steps

1. **Ground yourself cheaply.** Read the repo's CLAUDE.md and README. To
   find the affected area, spawn `sdlc-scout`, which runs on Haiku, with the
   feature description. Don't read the codebase broadly yourself.
2. **Resolve the load-bearing unknowns** in one `AskUserQuestion`, with
   four questions or fewer and concrete options. For anything with a
   sensible default, pick it and list it under *Constraints*.
3. **Write spec.md** from the template. Every acceptance criterion must be
   checkable by a test or a scripted smoke check.
4. **Get approval.** Show the AC list and ask whether to approve or change
   it. Once the user approves:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve spec --note "<user's words>"
   ```
   Then hand off to `sdlc-plan`.

## Template

```markdown
# Spec: <title>

## Problem
<1–3 sentences: who hurts, how, why now.>

## Context
<Existing behaviour and links to code (from the scout). Facts, not plans.>

## Acceptance criteria
- AC1: Given <state>, when <action>, then <observable result>.

## Non-goals
## Constraints
<Platform, compatibility, privacy (real finance data stays local), defaults chosen.>

## Open questions
```

Aim for 3–8 acceptance criteria. More than that usually means two features,
so suggest splitting. Leave out implementation detail unless it is itself
the requirement.
