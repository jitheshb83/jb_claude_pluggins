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
touch tests/__init__.py
cat > tests/test_calc.py <<'PY'
import unittest

from calc import add, subtract


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self):
        self.assertEqual(subtract(5, 3), 2)
PY
echo "# calc" > README.md
git add -A && git commit -qm "initial calc"
remote="$(mktemp -d "${TMPDIR:-/tmp}/sdlc-eval.XXXXXX")/remote.git"
git init -q --bare "$remote"
git remote add origin "$remote"
git push -q -u origin main
git checkout -q -b feature/work

printf '\n\ndef multiply(a, b):\n    return a * b\n' >> calc.py
git commit -qam "add multiply"
