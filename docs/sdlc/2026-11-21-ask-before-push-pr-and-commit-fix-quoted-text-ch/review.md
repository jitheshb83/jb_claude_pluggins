# Review: Ask before push, PR and commit; fix quoted-text check

Diff: main...feature/ask-before-push-pr-and-commit-fix-quoted-text-ch @ 6547d6e · reviewers: sdlc-reviewer, security-review

## Acceptance criteria
| AC | Status | Evidence |
|---|---|---|
| AC1 | Met | test_pr_create_with_parens_in_quoted_title_asks |
| AC2 | Met | test_substitution_and_compound_pr_create_still_refused (16 refused forms after fix 2) |
| AC3 | Met | test_plain_commit_asks |
| AC4 | Met | test_refused_commit_is_not_downgraded_to_ask, test_commit_on_merged_feature_branch_is_refused_not_asked |
| AC5 | Met | test_allow_push_env_adds_no_commit_prompt |
| AC6 | Met | gate.py messages, sdlc-ship steps 5-6, README row, version 0.1.2; plugin validate passed |

## Findings
| # | Severity | Where | Finding | Resolution |
|---|---|---|---|---|
| 1 | Important | tests: refused-commit test | Vacuous first assertion; merged-branch case untested | Fixed in 6547d6e |
| 2 | Nit | tests: AC2 | Missing hostile-input cases | Fixed in 6547d6e (6 cases, all refused) |
| 3 | Nit | sdlc-ship step 5 | `git add -A && git commit` skips the commit prompt | Fixed: two plain commands |
| 4 | Nit | publish.py push remote | `ext::` remote not validated; pre-existing, git blocks ext by default | Deferred: follow-up |
| 5 | Nit | gate.py:174 | Long wrapped refusal string | Deferred: cosmetic |
| S | None | security-review | No high-confidence vulnerabilities in has_shell_meta or in the order of ask versus refusals | n/a |

Fix round 1 changed only tests and a skill file, so the reviewer was not re-run; the suite was re-run (122 passed).
Not verified: the real Claude Code permission prompt.

## Verdict: PASS
