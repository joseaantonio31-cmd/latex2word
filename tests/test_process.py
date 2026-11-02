from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from latex2word import process
from latex2word.process import run_command


def test_captures_stdout_stderr_and_exit_code():
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_command([sys.executable, "-c", code], timeout=30)
    assert (result.stdout.strip(), result.stderr.strip(), result.returncode) == ("out", "err", 3)
    assert result.ok is False and result.timed_out is False and result.error is None


def test_timeout_is_reported_not_raised():
    code = "import sys, time; print('partial', flush=True); time.sleep(30)"
    result = run_command([sys.executable, "-c", code], timeout=1.5)
    assert result.timed_out is True
    assert result.returncode is None
    assert result.ok is False


def test_missing_executable_is_reported_not_raised(tmp_path: Path):
    result = run_command([str(tmp_path / "no-such-tool")], timeout=5)
    assert result.error is not None
    assert result.returncode is None and result.ok is False


def test_stdin_is_closed_so_filters_cannot_hang():
    code = "import sys; print(repr(sys.stdin.read()))"
    result = run_command([sys.executable, "-c", code], timeout=30)
    assert result.stdout.strip() == "''"


def test_arguments_are_passed_verbatim_without_a_shell():
    tricky = "a b & echo injected; $HOME"
    result = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", tricky], timeout=30)
    assert result.stdout.strip() == tricky


def test_subprocess_is_called_with_argv_list_and_no_shell(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, Any] = {}

    def fake_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["args"], seen["kwargs"] = args, kwargs
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    run_command(["tool", "--version"], timeout=5)
    assert seen["args"] == ["tool", "--version"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 5
    assert seen["kwargs"]["capture_output"] is True


@pytest.mark.parametrize(("args", "timeout"), [([], 5), (["x"], 0), (["x"], -1)])
def test_invalid_arguments_rejected(args: list[str], timeout: float):
    with pytest.raises(ValueError):
        run_command(args, timeout=timeout)
