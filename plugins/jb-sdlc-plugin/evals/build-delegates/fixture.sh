#!/bin/bash
# Shared fixture: a tiny Python repo on a fresh feature branch with a remote.
set -euo pipefail
git init -q -b main .
git config user.email eval@example.com
git config user.name eval
cat > calc.py <<'PY'
"""Tiny calculator used by the SDLC evals."""


def add(a, b):
    """Return a plus b."""
    return a + b


def subtract(a, b):
    """Retrun a minus b."""
    return a - b
PY
mkdir -p tests
cat > tests/test_calc.py <<'PY'
from calc import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
PY
echo "# calc" > README.md
git add -A && git commit -qm "initial calc"
git init -q --bare "$TMPDIR/eval-remote.git"
git remote add origin "$TMPDIR/eval-remote.git"
git push -q -u origin main
git checkout -q -b feature/work

mkdir -p docs/sdlc/2026-01-01-divide
cat > docs/sdlc/2026-01-01-divide/plan.md <<'PLAN'
# Plan (lite): Divide

## Goal
- Add divide(a, b) that raises ValueError when b == 0.

## Evidence
- calc.py:9 subtract() shows the style to follow; tests/test_calc.py holds the tests.

## Acceptance criteria
- AC1: divide(6, 3) == 2
- AC2: divide(1, 0) raises ValueError

## Changes
| File | Change |
|---|---|
| calc.py | add divide |
| tests/test_calc.py | add two tests |

## Steps
1. Write the two failing tests, then implement divide (verify: python3 -m pytest -q)

## Test plan
| AC1 | tests/test_calc.py::test_divide | unit |
| AC2 | tests/test_calc.py::test_divide_by_zero | unit |

## Verification
- `python3 -m pytest -q`

## Decisions
- error type -> ValueError (user)

## Open questions
None
PLAN
h=$(shasum -a 256 docs/sdlc/2026-01-01-divide/plan.md | cut -d" " -f1)
cat > docs/sdlc/2026-01-01-divide/state.json <<STATE
{"title": "Divide", "slug": "divide", "track": "lite", "branch": "feature/work", "base": "main",
 "created": "2026-01-01T00:00:00+00:00",
 "approvals": {"plan": {"at": "2026-01-01T00:00:00+00:00", "note": "user: approved", "sha256": "$h"}},
 "history": []}
STATE
git add -A && git commit -qm "docs: approved plan"
