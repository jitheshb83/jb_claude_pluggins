---
type: llm
focus: { source: file, path: calc.py }
---
PASS if calc.py defines divide(a, b) that returns a / b and raises ValueError when b == 0, with no narrating comments.
FAIL otherwise.
