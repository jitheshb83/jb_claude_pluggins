#!/usr/bin/env python3
"""UserPromptSubmit router for jb-sdlc-plugin.

Adds one short line of context (plain stdout on exit 0) when either:
  * the current branch has an active SDLC feature  -> its phase, so work resumes
    in the right skill even after /clear, or
  * the prompt reads like a development task        -> a pointer to the sdlc skill.
Otherwise prints nothing, so unrelated prompts cost zero extra tokens.
SDLC_AUTOROUTE=off disables it. Never blocks a prompt.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sdlc  # noqa: E402

DEV_INTENT = re.compile(
    r"(?:\A\s*(?:please\s+)?(?:implement|develop|refactor|build)\b)|\b("
    r"(?:please|can you|could you|let'?s|i want to|we need to|need to|help me)\s+"
    r"(?:implement|develop|refactor|build)\b|"
    r"(?:implement|develop|refactor)\s+(?:a|an|the|this|that|my|our|new|support|it)\b|"
    r"build (?:a|an|the|me|out)\b|"
    r"add (?:a |an |the |support |new )?(?:feature|option|command|endpoint|screen|test|skill|agent|hook|flag|setting)s?|"
    r"fix(?:es|ing)? (?:the |a |this |that )?(?:bug|issue|crash|error|failing|regression|test)|"
    r"(?:take|pick)(?: up| on)? (?:this|the|a|my|next) (?:task|ticket|issue|story)|"
    r"work on (?:this|the|a|issue|ticket|task)|"
    r"(?:write|create|make) (?:a |an |the )?(?:script|module|function|class|app|plugin|skill|agent|cli|tool|feature|api)\b|"
    r"migrat(?:e|ion) (?:the |to )|"
    r"ship (?:it|this|the)"
    r")",
    re.IGNORECASE,
)
QUESTION_ONLY = re.compile(r"^\s*(what|why|how does|how do|explain|can you explain|is there|does)\b", re.I)


def message(prompt: str, cwd: Path) -> str | None:
    if not prompt or prompt.lstrip().startswith("/"):
        return None  # slash commands route themselves
    root = sdlc.repo_root(cwd)
    feat = sdlc.active_feature(root) if root else None
    if feat:
        return (f"[jb-sdlc] Branch '{feat['branch']}' has an active SDLC feature "
                f"\"{feat['title']}\" ({feat.get('track', 'full')} track), phase: "
                f"{sdlc.phase(feat)}. Continue it via the sdlc skill unless this prompt is unrelated.")
    if DEV_INTENT.search(prompt) and not QUESTION_ONLY.match(prompt):
        where = f" in {root.name}" if root else ""
        return (f"[jb-sdlc] This reads like a development task{where}. The user's standing "
                f"preference is to run development work through the sdlc skill, which sizes it "
                f"(quick / lite / full) and picks models per phase.")
    return None


def main() -> None:
    if os.environ.get("SDLC_AUTOROUTE", "").lower() in ("off", "0", "false"):
        return
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    msg = message(event.get("prompt", ""), Path(event.get("cwd") or os.getcwd()))
    if msg:
        print(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a router bug must never block a prompt
        pass
    sys.exit(0)
