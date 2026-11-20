#!/usr/bin/env python3
"""SessionStart: route this session's git through the plugin's git hooks.

Writes shell code to $CLAUDE_ENV_FILE, which Claude Code runs before every Bash
command (main session and subagents; verified live). It appends a command-scope
`core.hooksPath` via GIT_CONFIG_COUNT/KEY/VALUE, so it affects only this
session's git processes — no repo or global config is touched. The hooks chain
to each repo's own hooks. SDLC_GATES=off at command time disables it.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "githooks"

SNIPPET = """\
if [ -z "${{SDLC_GITHOOKS:-}}" ] && [ "${{SDLC_GATES:-on}}" != off ]; then
  _sdlc_n=${{GIT_CONFIG_COUNT:-0}}
  export "GIT_CONFIG_KEY_${{_sdlc_n}}=core.hooksPath" "GIT_CONFIG_VALUE_${{_sdlc_n}}="{hooks}
  export GIT_CONFIG_COUNT=$((_sdlc_n + 1)) SDLC_GITHOOKS={hooks}
  unset _sdlc_n
fi
"""


def main() -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or os.environ.get("SDLC_GATES", "").lower() in ("off", "0", "false"):
        return
    for f in HOOKS.iterdir():  # plugin copies can lose the exec bit; git silently skips such hooks
        if f.is_file():
            f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(env_file, "a") as fh:
        fh.write(SNIPPET.format(hooks=shlex.quote(str(HOOKS))))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a broken installer must not stop the session from starting
        print(f"jb-sdlc: git hooks not installed: {e}", file=sys.stderr)
    sys.exit(0)
