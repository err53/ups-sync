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
        "git commit -n -m x",
        "git commit -anm x",
        f"cd repo && git commit {FLAG}",
        f"git add -A; git commit {FLAG} -m x",
    ],
)
def test_blocks_skipping_hooks(command):
    assert guard.skips_hooks(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        f'git commit -m "document {FLAG}"',
        "git commit -m -n",
        "git push -n origin main",  # dry run
        "git config core.hooksPath .githooks",
        f'echo "never use {FLAG}" > notes.txt',
        f"ls; echo {FLAG}",
        f"git log --grep {FLAG}",
        'git commit -m "unbalanced',
    ],
)
def test_allows_everything_else(command):
    assert not guard.skips_hooks(command)
