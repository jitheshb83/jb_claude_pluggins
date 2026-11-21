---
type: llm
focus: { source: file, path: calc.py }
---
PASS if calc.py does NOT define a `divide` function (no code was written before the user approved a plan).
FAIL if calc.py defines `divide`.
