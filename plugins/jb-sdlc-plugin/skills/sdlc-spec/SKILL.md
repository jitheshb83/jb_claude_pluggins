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
   Treat the scout's map as leads, not evidence. Before citing anything as
   fact, open the cited lines yourself.
2. **Discuss before you write.** Give the user, briefly:
   - your understanding of the request;
   - what the code shows today, each item with its `path:line` or
     `command` evidence;
   - the questions that decide the acceptance criteria.

   Ask in one `AskUserQuestion` with four questions or fewer and concrete
   options. Don't fill gaps with assumptions. Anything you can't verify is a
   question. A sensible default goes in as a question with that default
   listed first, not as a silent choice.
3. **Write spec.md** from the template. Record each answer under
   `## Decisions` as "question → user's answer". Leave `## Open questions` as
   `None` only when nothing is unclear. Every acceptance criterion must be
   checkable by a test or a scripted smoke check.
4. **Draw the user flow.** Add a Mermaid flow diagram showing the happy
   path and the main error branch, plus a state diagram if the feature has
   states. Follow the house style and templates in `${CLAUDE_PLUGIN_ROOT}/references/diagrams.md`: the init line,
   classed nodes, at most 15 nodes. The diagram replaces prose rather than
   repeating it. Then run:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" check-diagrams
   ```
5. **Get approval.** `approve spec` refuses when sections are empty, when
   Evidence cites nothing actually read or run, when an open question
   remains, or when `--note` is missing. Show the AC list and link `spec.md`. The diagram
   renders in VS Code's Markdown preview (with a Mermaid extension) and on
   GitHub. Ask whether to approve or change it. Once the user approves:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve spec --note "<user's words>"
   ```
   Then hand off to `sdlc-plan`.

## Template

~~~markdown
# Spec: <title>

## Problem
<1–3 sentences: who hurts, how, why now.>

## User flow
```mermaid
<flowchart LR: actor -> steps -> outcome, plus the error branch (template in references/diagrams.md)>
```

## Context
<Existing behaviour, as facts, not plans.>

## Evidence
- <path:line or `command`>: <what it shows>. One bullet per claim made above.

## Acceptance criteria
- AC1: Given <state>, when <action>, then <observable result>.

## Non-goals
## Constraints
<Platform, compatibility, privacy (real finance data stays local).>

## Decisions
- <question> → <user's answer>

## Open questions
None
~~~

Aim for 3–8 acceptance criteria. More than that usually means two features,
so suggest splitting. Leave out implementation detail unless it is itself
the requirement.
