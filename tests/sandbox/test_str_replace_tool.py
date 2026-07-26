"""Unit tests for the ``str_replace`` sandbox tool.

These tests isolate the tool's own guard clauses (empty ``old_str``, empty
file, missing substring, normal replace) from sandbox/thread-data plumbing by
stubbing ``ensure_sandbox_initialized`` and using a runtime whose sandbox
state is absent, which makes ``is_local_sandbox`` return False and skips the
local-path validation/resolution branch entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deerflow.sandbox import tools as sandbox_tools
from deerflow.sandbox.tools import str_replace_tool


class FakeSandbox:
    """Minimal duck-typed sandbox capturing read/write calls."""

    def __init__(self, content: str | None) -> None:
        self.id = "fake-sandbox"
        self._content = content
        self.write_calls: list[tuple[str, str]] = []

    def read_file(self, path: str) -> str | None:
        return self._content

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self.write_calls.append((path, content))
        self._content = content


def _runtime() -> SimpleNamespace:
    # Empty state -> is_local_sandbox() returns False, so the tool skips
    # local path validation/resolution and uses `path` as-is.
    return SimpleNamespace(context={"thread_id": "thread-1"}, config={}, state={})


def _install_fake_sandbox(monkeypatch: pytest.MonkeyPatch, content: str | None) -> FakeSandbox:
    fake_sandbox = FakeSandbox(content)
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized", lambda runtime: fake_sandbox)
    return fake_sandbox


def test_str_replace_empty_old_str_returns_error_and_does_not_modify_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = _install_fake_sandbox(monkeypatch, "hello world")

    result = str_replace_tool.func(
        _runtime(),
        description="try to insert via empty old_str",
        path="/mnt/user-data/workspace/file.txt",
        old_str="",
        new_str="INJECTED",
        replace_all=False,
    )

    assert result.startswith("Error:")
    assert "old_str" in result
    # File must be untouched: no write call, content unchanged.
    assert fake_sandbox.write_calls == []
    assert fake_sandbox._content == "hello world"


def test_str_replace_empty_file_with_nonempty_old_str_returns_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = _install_fake_sandbox(monkeypatch, "")

    result = str_replace_tool.func(
        _runtime(),
        description="replace in an empty file",
        path="/mnt/user-data/workspace/empty.txt",
        old_str="anything",
        new_str="something",
        replace_all=False,
    )

    assert result == "Error: String to replace not found in file: /mnt/user-data/workspace/empty.txt"
    assert fake_sandbox.write_calls == []


def test_str_replace_missing_substring_returns_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = _install_fake_sandbox(monkeypatch, "hello world")

    result = str_replace_tool.func(
        _runtime(),
        description="replace a substring that isn't present",
        path="/mnt/user-data/workspace/file.txt",
        old_str="goodbye",
        new_str="hi",
        replace_all=False,
    )

    assert result == "Error: String to replace not found in file: /mnt/user-data/workspace/file.txt"
    assert fake_sandbox.write_calls == []


def test_str_replace_replaces_first_occurrence_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = _install_fake_sandbox(monkeypatch, "foo bar foo")

    result = str_replace_tool.func(
        _runtime(),
        description="replace first occurrence",
        path="/mnt/user-data/workspace/file.txt",
        old_str="foo",
        new_str="baz",
        replace_all=False,
    )

    assert result == "OK"
    assert fake_sandbox.write_calls == [("/mnt/user-data/workspace/file.txt", "baz bar foo")]


def test_str_replace_replace_all_replaces_every_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_sandbox = _install_fake_sandbox(monkeypatch, "foo bar foo")

    result = str_replace_tool.func(
        _runtime(),
        description="replace every occurrence",
        path="/mnt/user-data/workspace/file.txt",
        old_str="foo",
        new_str="baz",
        replace_all=True,
    )

    assert result == "OK"
    assert fake_sandbox.write_calls == [("/mnt/user-data/workspace/file.txt", "baz bar baz")]
