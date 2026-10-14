"""Spoken alerts: speak() says what went wrong instead of failing silently."""

import subprocess
import sys
from unittest import mock

import pytest

from gui.message_viewer import speak


def _done(code=0, err=b""):
    return subprocess.CompletedProcess([], code, stdout=b"", stderr=err)


def test_spoken_returns_none():
    with mock.patch("subprocess.run", return_value=_done()) as run:
        assert speak("Hello", 0.5) is None
    assert "Hello" in " ".join(run.call_args.args[0])


def test_failure_is_reported_with_the_first_error_line():
    err = b"Add-Type : Cannot find System.Speech\r\nAt line:1 char:1\r\n"
    with mock.patch("subprocess.run", return_value=_done(1, err)):
        assert speak("Hello", 0.5) == "Add-Type : Cannot find System.Speech"


def test_failure_without_output_gives_the_exit_code():
    with mock.patch("subprocess.run", return_value=_done(3)):
        assert speak("Hello", 0.5) == "exit code 3"


def test_timeout_is_reported():
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired("x", 20)):
        assert "20 seconds" in speak("Hello", 0.5)


def test_quotes_cannot_break_out_of_the_command():
    with mock.patch("subprocess.run", return_value=_done()) as run:
        speak("it's \"here\"", 0.5)
    assert "its here" in " ".join(run.call_args.args[0])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows speech")
def test_windows_uses_powershell_with_volume():
    with mock.patch("subprocess.run", return_value=_done()) as run:
        speak("Hello", 0.85)
    cmd = run.call_args.args[0]
    assert cmd[0].lower().endswith("powershell.exe")
    assert "$s.Volume = 85" in cmd[-1]
