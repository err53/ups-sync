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

HEREDOC_OP = re.compile(r"<<(-?)[ \t]*(['\"]?)([^\s'\"<>;&|()]+)\2")
SKIP = re.compile(
    r"\bgit\b.*\b(?:commit|push)\b.*(?<![\w-])--no-verify(?![\w-])", re.DOTALL
)


def skips_hooks(command: str) -> bool:
    return bool(SKIP.search(_strip_text(command)))


def _strip_text(cmd: str) -> str:
    """The command with quoted strings emptied and heredoc bodies removed.

    One left-to-right pass, so quotes and heredocs are each recognized only
    outside the other. A heredoc's opener line is kept; its body runs to a
    line that is exactly the delimiter (leading tabs allowed for <<-), or to
    the end, as in bash.
    """
    out: list[str] = []
    pending: list[tuple[bool, str]] = []  # heredocs whose bodies start next line
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in "'\"":
            j = i + 1
            while j < n and cmd[j] != c:
                j += 2 if c == '"' and cmd[j] == "\\" else 1
            out.append("''")
            i = j + 1
        elif cmd.startswith("\\\n", i):  # line continuation: same command line
            out.append(" ")
            i += 2
        elif cmd.startswith("<<<", i):  # here-string, not a heredoc
            out.append("<<<")
            i += 3
        elif m := HEREDOC_OP.match(cmd, i):
            pending.append((m.group(1) == "-", m.group(3)))
            out.append(" ")
            i = m.end()
        elif c == "\n" and pending:
            out.append("\n")
            i += 1
            for dash, delim in pending:
                while i < n:
                    end = cmd.find("\n", i)
                    end = n if end == -1 else end
                    line = cmd[i:end]
                    i = end + 1
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
            pending = []
        else:
            out.append(c)
            i += 1
    return "".join(out)


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
