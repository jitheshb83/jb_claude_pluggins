#!/usr/bin/env python3
"""PostToolUse output-token watchdog for jb-sdlc-plugin.

After Edit/Write/MultiEdit on a code file, flags output that cost tokens
without adding value, and feeds a short note back to Claude:
  * comment-heavy additions, or comments that narrate the code
  * commented-out code
  * a whole-file Write that changed only a small part of a tracked file
Silent when nothing is wrong. Never blocks. SDLC_LEAN=off disables.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HASH = {".py", ".sh", ".bash", ".zsh", ".rb", ".yaml", ".yml", ".toml", ".r", ".pl"}
SLASH = {".kt", ".kts", ".java", ".js", ".jsx", ".ts", ".tsx", ".go", ".swift", ".c", ".h",
         ".cc", ".cpp", ".hpp", ".cs", ".rs", ".scala", ".gradle", ".php", ".dart", ".m"}

NARRATION = re.compile(
    r"^(?:now |then |next |here we |this (?:function|method|class|line|block|code) )?"
    r"(?:import|define|create|initiali[sz]e|init|set|get|return|call|loop(?: over| through)?|"
    r"iterate(?: over| through)?|increment|decrement|check(?: if| whether)?|add|append|"
    r"print|update|assign|declare|instantiate|open|close|read|write|handle|parse)s?\b",
    re.I,
)
CODEISH = re.compile(r"(?:[;{]\s*$|^\w[\w.\[\]]*\s*=[^=]|"
                     r"^(?:def|class|if|for|while|return|import|from|val|var|let|const|fun|func|"
                     r"public|private)\b.*[(:={]|^[\w.]+\(.*\)$)")


def comment_text(line: str, ext: str) -> str | None:
    s = line.strip()
    if ext in HASH:
        if s.startswith("#!") or s.startswith("# -*-") or re.match(r"#\s*(type|noqa|pragma|fmt|pylint)\b", s):
            return None
        return s[1:].strip() if s.startswith("#") else None
    if ext in SLASH:
        for p in ("//", "/*", "*/"):
            if s.startswith(p):
                return s[len(p):].strip().rstrip("*/").strip()
        if s == "*" or s.startswith("* "):  # block-comment continuation, not `*p = x;`
            return s[1:].strip()
    return None


def added_lines(tool: str, inp: dict) -> list[str]:
    if tool == "Write":
        return (inp.get("content") or "").splitlines()
    pairs = inp.get("edits") if tool == "MultiEdit" else [inp]
    out: list[str] = []
    for e in pairs or []:
        old = set((e.get("old_string") or "").splitlines())
        out += [ln for ln in (e.get("new_string") or "").splitlines() if ln not in old]
    return out


def rewrite_ratio(path: Path) -> tuple[int, int] | None:
    """(changed, total) lines for a tracked file just overwritten via Write."""
    try:
        top = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch", path.name],
                             capture_output=True, text=True, timeout=3)
        if top.returncode != 0:
            return None
        stat = subprocess.run(["git", "-C", str(path.parent), "diff", "--numstat", "HEAD", "--", path.name],
                              capture_output=True, text=True, timeout=3).stdout.split()
        total = sum(1 for _ in path.open(errors="ignore"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if len(stat) < 2 or not stat[0].isdigit():
        return None
    return int(stat[0]) + int(stat[1]), total


def review(tool: str, inp: dict) -> list[str]:
    fp = inp.get("file_path") or ""
    ext = Path(fp).suffix.lower()
    if ext not in HASH | SLASH:
        return []
    notes = []
    lines = [ln for ln in added_lines(tool, inp) if ln.strip()]
    comments = [c for c in (comment_text(ln, ext) for ln in lines) if c]
    narrating = [c for c in comments if NARRATION.match(c) and len(c.split()) <= 7]
    dead = [c for c in comments if CODEISH.search(c)]
    if len(lines) >= 8 and len(comments) / len(lines) > 0.3:
        notes.append(f"{len(comments)} of {len(lines)} added lines are comments")
    if len(narrating) >= 2:
        sample = "; ".join(f'"{c[:40]}"' for c in narrating[:2])
        notes.append(f"{len(narrating)} comments restate the code ({sample})")
    if len(dead) >= 2:
        notes.append(f"{len(dead)} lines look like commented-out code")
    if tool == "Write":
        r = rewrite_ratio(Path(fp))
        if r and r[1] >= 60 and r[0] <= 0.25 * r[1]:
            notes.append(f"whole-file Write changed only {r[0]} of {r[1]} lines — use Edit for partial changes")
    return notes


def main() -> None:
    if os.environ.get("SDLC_LEAN", "").lower() in ("off", "0", "false"):
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    tool = event.get("tool_name", "")
    if tool not in ("Edit", "Write", "MultiEdit"):
        return
    notes = review(tool, event.get("tool_input") or {})
    if notes:
        msg = (f"[jb-sdlc lean] {Path(event['tool_input'].get('file_path', '')).name}: "
               + "; ".join(notes)
               + ". Output tokens are the most expensive kind: comment only the non-obvious why, "
                 "never narrate code, delete dead code instead of commenting it out. "
                 "Apply this to the next edits; only clean up this one if it's egregious.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
