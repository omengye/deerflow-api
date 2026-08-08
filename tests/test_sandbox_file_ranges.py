import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from deerflow.sandbox.aio import AioSandbox
from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.search import GrepMatch, find_grep_matches
from deerflow.sandbox.tools import read_file_tool


def test_grep_accepts_single_file_path(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text("first\nRevenue grew 20%\n", encoding="utf-8")

    matches, truncated = find_grep_matches(target, "Revenue")

    assert matches == [GrepMatch(path=str(target), line_number=2, line="Revenue grew 20%")]
    assert truncated is False


def test_local_sandbox_streams_open_and_closed_line_ranges(tmp_path: Path) -> None:
    target = tmp_path / "range.txt"
    target.write_text("\n".join(f"line {number}" for number in range(1, 11)), encoding="utf-8")
    sandbox = LocalSandbox("local", [PathMapping(container_path="/mnt/data", local_path=str(tmp_path))])

    assert sandbox.read_file("/mnt/data/range.txt", start_line=3, end_line=5) == "line 3\nline 4\nline 5"
    assert sandbox.read_file("/mnt/data/range.txt", start_line=8) == "line 8\nline 9\nline 10"
    assert sandbox.read_file("/mnt/data/range.txt", end_line=2) == "line 1\nline 2"


def test_read_file_tool_validates_and_pushes_range_to_sandbox(monkeypatch) -> None:
    calls: list[tuple[str, int | None, int | None]] = []

    class RangeSandbox:
        def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
            calls.append((path, start_line, end_line))
            return "selected"

    runtime = SimpleNamespace(context={"thread_id": "thread-1"}, config={}, state={})
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda _runtime: RangeSandbox())
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda _runtime: None)

    result = read_file_tool.func(
        runtime,
        description="read range",
        path="/mnt/user-data/workspace/large.log",
        start_line=10,
        end_line=20,
    )
    invalid = read_file_tool.func(
        runtime,
        description="read invalid range",
        path="/mnt/user-data/workspace/large.log",
        start_line=0,
    )

    assert result == "selected"
    assert calls == [("/mnt/user-data/workspace/large.log", 10, 20)]
    assert "start_line must be >= 1" in invalid


def test_aio_single_file_grep_uses_process_safe_ignore_separator(monkeypatch) -> None:
    sandbox = AioSandbox("probe", "unused")
    captured: dict[str, object] = {}

    def fake_exec(argv, *, input_data=None, text=True):
        captured["argv"] = argv
        captured["script"] = input_data
        payload = {"truncated": False, "matches": [{"path": "/tmp/report.md", "line_number": 1, "line": "needle"}]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(sandbox, "_docker_exec", fake_exec)
    matches, truncated = sandbox.grep("/tmp/report.md", "needle")

    assert matches == [GrepMatch(path="/tmp/report.md", line_number=1, line="needle")]
    assert truncated is False
    assert all("\0" not in argument for argument in captured["argv"])
    assert "root_is_file = os.path.isfile(root)" in captured["script"]


def test_aio_directory_helpers_never_pass_nul_in_process_arguments(monkeypatch) -> None:
    sandbox = AioSandbox("probe", "unused")
    calls: list[list[str]] = []

    def fake_exec(argv, *, input_data=None, text=True):
        calls.append(argv)
        stdout = "0\n" if argv[2:3] == ["-"] and len(argv) >= 8 else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(sandbox, "_docker_exec", fake_exec)
    assert sandbox.list_dir("/tmp") == []
    assert sandbox.glob("/tmp", "**/*.py") == ([], False)

    assert calls
    assert all("\0" not in argument for argv in calls for argument in argv)


def test_aio_path_mutations_use_argument_safe_python_helpers(monkeypatch) -> None:
    sandbox = AioSandbox("probe", "unused")
    calls: list[tuple[list[str], str | bytes | None]] = []

    def fake_exec(argv, *, input_data=None, text=True):
        calls.append((argv, input_data))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "_docker_exec", fake_exec)

    sandbox.delete_path("/mnt/user-data/workspace/old file.txt", recursive=False)
    sandbox.move_path(
        "/mnt/user-data/workspace/source file.txt",
        "/mnt/user-data/workspace/archive/final file.txt",
        overwrite=True,
    )

    assert calls[0][0] == [
        "python3",
        "-",
        "/mnt/user-data/workspace/old file.txt",
        "0",
    ]
    assert calls[1][0] == [
        "python3",
        "-",
        "/mnt/user-data/workspace/source file.txt",
        "/mnt/user-data/workspace/archive/final file.txt",
        "1",
    ]
    assert all(isinstance(script, str) and "shutil" in script for _, script in calls)
