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

printf '\n\ndef multiply(a, b):\n    return a * b\n' >> calc.py
git commit -qam "add multiply"
