"""Claude Code PreToolUse hook: refuse `git commit` or `git push` with --no-verify.

Reads the Bash tool call as JSON on stdin and exits 2 (block) when a git
commit or push in the command skips hooks. Text that only mentions the flag,
such as a commit message, is allowed.
"""

import json
import shlex
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}


def skips_hooks(command: str) -> bool:
    # Non-POSIX mode keeps quotes on tokens, so a quoted ";" is not a
    # separator; newlines are separators, not whitespace.
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: let the shell report it
        return False
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token not in SEPARATORS:
            segment.append(_unquote(token))
            continue
        if _segment_skips(segment):
            return True
        segment = []
    return False


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _segment_skips(args: list[str]) -> bool:
    # git must be the command itself, after any VAR=value assignments.
    while args and "=" in args[0] and not args[0].startswith("-"):
        args = args[1:]
    if not args or args[0] != "git":
        return False
    args = args[1:]
    # Skip global options such as -C <dir> or -c key=value.
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-C", "-c") else args[1:]
    if not args or args[0] not in ("commit", "push"):
        return False
    sub, rest = args[0], args[1:]
    for i, arg in enumerate(rest):
        if i and rest[i - 1] in ("-m", "-F", "-C", "-c", "--message", "--file"):
            continue  # this is a value, such as the commit message
        if arg == "--":
            break
        if arg == "--no-verify":
            return True
        # `git commit -n` is the short form; for push, -n means --dry-run.
        if sub == "commit" and arg.startswith("-") and not arg.startswith("--"):
            flags = arg[1:]
            cut = min((flags.find(c) for c in "mFCc" if c in flags), default=len(flags))
            if "n" in flags[:cut]:
                return True
    return False


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
