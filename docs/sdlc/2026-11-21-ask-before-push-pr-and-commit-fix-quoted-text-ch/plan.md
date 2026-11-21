# Plan: Ask before push, PR and commit; fix quoted-text check

## Goal
Keep the human in the loop without blocking: plain `git push`, `gh pr create` and `git commit` show a permission prompt, including when a title or message has `( ) ; & | < >` inside quotes. Refusals for unsafe forms stay.

## Acceptance criteria
- AC1: Given `gh pr create --title "feat(ci): x" --body-file f`, when the gate runs, then it answers "ask", not a refusal.
- AC2: Given a command that really is compound or substitutes (`gh pr create; rm x`, `gh pr create --title "$(id)"`, backticks, `\`), then it is still refused.
- AC3: Given a plain `git commit -m "feat(x): y"` on a fresh feature branch with the plan approved, then the gate answers "ask".
- AC4: Given a commit that is already refused (main, merged branch, plan not approved), then it is still refused, not downgraded to "ask".
- AC5: Given SDLC_ALLOW_PUSH is set by the user, then no commit/push/PR prompt is added by the gate.
- AC6: Refusal messages, `sdlc-ship` step 6 and the README describe the ask flow; version bumped to 0.1.2.

## Evidence
- hooks/publish.py:15,22: SHELL_META `[;&|<>`$()\\\n\r]` is tested against the whole command, quotes included, so `feat(ci): x` makes `ask_publish` return and the gate refuse.
- hooks/publish.py:28-29: plain `gh pr create` → `ask(...)`; hooks/publish.py:65-68 prints the "ask" decision JSON.
- hooks/gate.py:169-177: `ask_publish` runs only when SDLC_ALLOW_PUSH is unset; otherwise `git push` and `gh pr` are refused.
- hooks/gate.py:187-199: commits are refused on base/merged branch or before plan approval; no ask path exists.
- skills/sdlc-ship/SKILL.md:47-53: step 6 leads with the `! SDLC_ALLOW_PUSH=1 …` hand-over and mentions the plain ask form only as an alternative.
- README.md:133: the "Ask before publishing" row covers push and PR only.
- tests/test_sdlc.py:1031-1062: `TestAskBeforePublish` and `gate()` helper are the pattern to extend.
- `python3 -m pytest tests -q` → 116 passed (65s) on main, before changes.
- Session: the gate refused `gh pr create --title "feat(ci): …"` and allowed the same command with `feat: …` to reach the prompt.

## Approach
In `publish.py`, test metacharacters on the command with single-quoted spans removed and with `( ) ; & | < >` ignored inside double quotes; `$`, backtick and `\` stay flagged everywhere outside single quotes because they run inside double quotes. In `gate.py`, after the existing commit refusals pass, answer "ask" for a plain `git commit` when SDLC_ALLOW_PUSH is unset. Rejected alternative: wording/docs only, which leaves `feat(ci): …` titles refused.

## Changes
| File | Change |
|---|---|
| hooks/publish.py | Quote-aware metacharacter check; add `ask_commit` helper reusing `ask()` |
| hooks/gate.py | Call the commit ask after the commit refusals; update the refusal text for push and `gh pr` and the header comment |
| skills/sdlc-ship/SKILL.md | Step 6 leads with the plain ask form; `SDLC_ALLOW_PUSH` form is the fallback |
| README.md | "Ask before publishing" row: add commit and quoted-text note; note that a `deny` rule in settings.json pre-empts the hook's ask |
| tests/test_sdlc.py | New tests for AC1-AC5 in `TestAskBeforePublish` |
| .claude-plugin/plugin.json | version 0.1.1 → 0.1.2 |

## Steps
1. Red: add tests for AC1-AC5 (verify: `python3 -m pytest tests/test_sdlc.py -q -k AskBeforePublish` fails on the new tests only).
2. Quote-aware check in publish.py (verify: AC1, AC2 tests pass).
3. Commit ask in gate.py after the refusals (verify: AC3-AC5 pass).
4. Update messages, ship skill, README, version (verify: `claude plugin validate .`).
5. Full suite (verify: `python3 -m pytest tests -q`).

## Test plan
| AC | Test (file::name) | Kind |
|---|---|---|
| AC1 | tests/test_sdlc.py::TestAskBeforePublish::test_pr_create_with_parens_in_quoted_title_asks | unit |
| AC2 | tests/test_sdlc.py::TestAskBeforePublish::test_substitution_and_compound_pr_create_still_refused | unit |
| AC3 | tests/test_sdlc.py::TestAskBeforePublish::test_plain_commit_asks | unit |
| AC4 | tests/test_sdlc.py::TestAskBeforePublish::test_refused_commit_is_not_downgraded_to_ask | unit |
| AC5 | tests/test_sdlc.py::TestAskBeforePublish::test_allow_push_env_adds_no_commit_prompt | unit |
| AC6 | `claude plugin validate .` plus grep of the new text | smoke |

## Verification
- `python3 -m pytest tests -q`
- `claude plugin validate .`
- Smoke: pipe a `gh pr create --title "feat(ci): x"` PreToolUse payload into `hooks/gate.py` and confirm the "ask" JSON.

## Risks & rollback
`sdlc-builder` subagents commit locally, so each commit now prompts; set SDLC_ALLOW_PUSH=1 to avoid that, or revert the one gate.py call.

## Decisions
- Ask on commit too → Yes, ask on commit
- Scope → Full approach: quote-aware fix with tests, messages, ship skill, README, version bump

## Open questions
None
