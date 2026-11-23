"""The Claude Code hook that stops agents from skipping the pre-commit hook."""

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parent.parent / ".claude" / "hooks" / "no_verify_guard.py"
_spec = importlib.util.spec_from_file_location("no_verify_guard", _path)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

FLAG = "--no-" + "verify"  # split so this file's own text never trips the hook


@pytest.mark.parametrize(
    "command",
    [
        f"git commit {FLAG} -m x",
        f'git -C repo commit -am "x" {FLAG}',
        f"git push {FLAG} origin main",
        f"git -C x push -u origin b {FLAG}",
        f"cd repo && git commit {FLAG}",
        f"git add -A; git commit {FLAG} -m x",
        f"git status\ngit commit {FLAG}",
        f"git status &&\ngit commit {FLAG}",
        f"git status\n\ngit commit {FLAG}",
        f'git commit -m ";" {FLAG}',
        f"git commit -m -- {FLAG}",
        f'git commit -m "--message" {FLAG}',
        f"GIT_AUTHOR_NAME=x git commit {FLAG}",
        f'GIT_AUTHOR_NAME="Jane Doe" git commit {FLAG}',
        f"git commit -F - {FLAG} <<'EOF'\nmessage\nEOF",
        # Ambiguous, so blocked: the guard does not parse the shell.
        f"echo git commit {FLAG}",
    ],
)
def test_blocks_skipping_hooks(command):
    assert guard.skips_hooks(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        f'git commit -m "document {FLAG}"',
        f"git commit -m 'a; git commit {FLAG}'",
        f'git commit --message="Document {FLAG} behavior"',
        f"git commit -F - <<'EOF'\nNever use {FLAG}.\nEOF",
        f'git commit -F - <<EOF\nit\'s "{FLAG}"\nEOF\ngit log',
        "git config core.hooksPath .githooks",
        f'echo "never use {FLAG}" > notes.txt',
        f"ls; echo {FLAG}",
        f"git log --grep {FLAG}",
        f"git commit -m x {FLAG}-later",
    ],
)
def test_allows_everything_else(command):
    assert not guard.skips_hooks(command)
