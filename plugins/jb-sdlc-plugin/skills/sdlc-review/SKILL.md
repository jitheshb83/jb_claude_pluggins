---
name: sdlc-review
description: SDLC phase 4. Independent review of the feature branch against its plan and spec. Every track gets the sdlc-reviewer agent (Sonnet, high effort, fresh context); the full track adds the code-review skill, and security-review when relevant. Results go into review.md with a PASS or FAIL verdict. Use when the active SDLC feature is in the review phase, or the user asks to review the feature before shipping.
---

# Phase 4: Review

A reviewer that didn't write the code, in a fresh context. It also runs on a
different model from the one that planned, which catches different
mistakes.

## Steps

**One pass, then stop.** Reviews report findings in batches unless asked for
all of them. Chasing batches one at a time turns review into an endless
loop, so collect the complete list once and triage it once.

Steps 1–3 are independent, so **run them concurrently**. Spawn the reviewer
agent first (subagents run in the background), then invoke the review skills
while it works, and collect everything before step 4.

1. **Conformance and quality, on every track.** Spawn the `sdlc-reviewer`
   agent with the feature directory and base branch. It runs the suite and
   returns AC and findings tables. For a diff spanning separate areas, such
   as a backend and the Android app, spawn one reviewer per area in the same
   message.
2. **Correctness, full track only.** Invoke the `code-review` skill on the
   branch at level `high` with `--max-findings all`, so every finding comes
   back in one run. Never use `max` or `ultra` unless the user asks.
3. **Security, when the change touches** auth, tokens, MCP tool exposure,
   input-derived file paths, subprocess calls or network: invoke
   `security-review`, on any track.
4. **Triage every finding once** into one of these:
   - **bug**: fix it;
   - **false positive**: drop it, with one line on why;
   - **out of scope**: deliberate evasion of the guardrails, which the
     README threat model says the gates don't defend against. Document it
     under Follow-ups; don't fix it.

   Write `review.md` from the template.
5. **Important findings are build work, and the fix plan needs approval.**
   List the fixes you intend to make, one line each with its finding number,
   and get the user's yes before anything changes. Then run
   `sdlc.py reopen build --note "review fixes"`, re-spawn `sdlc-builder` with
   the findings list (one builder per lane when the fixes fall in disjoint
   files, run in parallel as in `sdlc-build`), re-record the build evidence, and then re-run only the
   reviewer agent on the new diff, not the whole review stack. **At most two
   fix rounds.** Anything still open after that goes to the user as a list,
   not into a third round.
6. When nothing important remains open, set `Verdict: PASS` and record it:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py" approve review --note "<n> fixed, <m> deferred"
   ```
   Continue with `sdlc-ship`.

## Template

```markdown
# Review: <title>

Diff: <base>...<branch> @ <short sha> · reviewers: sdlc-reviewer[, code-review, security-review]

## Acceptance criteria
| AC | Status | Evidence |
|---|---|---|

## Findings
| # | Severity | Where | Finding | Resolution |
|---|---|---|---|---|

## Verdict: FAIL   <!-- change to PASS only when no important finding is open -->
```

`approve review` refuses unless the file has a `Verdict: PASS` line. The
approval is tied to the file's contents, so editing `review.md` afterwards
re-opens the gate.
