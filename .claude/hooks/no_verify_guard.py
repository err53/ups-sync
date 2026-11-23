"""Claude Code PreToolUse hook: refuse `git commit` or `git push` with --no-verify.

Reads the Bash tool call as JSON on stdin and exits 2 (block) when the
command runs git commit or push and passes --no-verify anywhere after it.

This is a speed bump for an agent taking a shortcut, not a security
boundary, so it does not parse the shell. It removes quoted strings and
heredoc bodies, where the flag can only be text such as a commit message,
and blocks on what is left. Anything ambiguous is blocked.
"""

import json
import re
import sys

HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE
)
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
SKIP = re.compile(
    r"\bgit\b.*\b(?:commit|push)\b.*(?<![\w-])--no-verify(?![\w-])", re.DOTALL
)


def skips_hooks(command: str) -> bool:
    bare = QUOTED.sub("''", HEREDOC.sub("", command))
    return bool(SKIP.search(bare))


def main() -> int:
    command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    if skips_hooks(command):
        print(
            "Blocked: fix what the pre-commit hook reports instead of skipping it"
            " (scripts/check.sh --fix).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
