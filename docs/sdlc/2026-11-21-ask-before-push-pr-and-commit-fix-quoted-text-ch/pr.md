## Summary
- `gh pr create` and `git push` now reach the permission prompt even when a quoted title or message contains `( ) ; & | < >`. `$`, backticks, `\`, newlines and unbalanced quotes are still refused.
- A plain `git commit` now asks too, after the existing branch and plan-approval refusals. `SDLC_ALLOW_PUSH=1` skips the commit prompt.
- `sdlc-ship` leads with the plain ask flow, and commits as two separate commands so the prompt applies. README and refusal messages updated. Version 0.1.2.

## Acceptance criteria
- AC1 quoted title with parens gets "ask"; AC2 substitution/compound forms still refused; AC3 plain commit asks; AC4 refused commits are not downgraded; AC5 `SDLC_ALLOW_PUSH` adds no commit prompt; AC6 messages, ship skill, README and version updated.

## Links
- Plan: docs/sdlc/2026-11-21-ask-before-push-pr-and-commit-fix-quoted-text-ch/plan.md
- Review: docs/sdlc/2026-11-21-ask-before-push-pr-and-commit-fix-quoted-text-ch/review.md (PASS; 2 nits deferred)

## Evidence
`python3 -m pytest tests -q`: 122 passed; `claude plugin validate .`: passed. Not verified: the real Claude Code permission prompt.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
