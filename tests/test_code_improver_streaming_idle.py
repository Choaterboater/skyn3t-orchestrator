"""Tests for the streaming-idle timeout in CodeImproverAgent.

A flat ``subprocess.run(..., timeout=180)`` kills any build step that
takes longer than 3 minutes — even when it's making real progress (npm
install on a fresh tree, pytest on a fat suite). The streaming-idle
pattern replaces it with a two-tier cap: emit-anything within
``idle_timeout`` to keep going, ``hard_timeout`` is the absolute
ceiling. See the user-memory note ``feedback_cli_timeout_streaming``
for why we never go back.

These tests run real subprocesses (small, fast) so the contract is
exercised end-to-end — mock tests for this would just re-state the
implementation. Each test is bounded so the suite stays quick.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from skyn3t.agents.code_improver import CodeImproverAgent


class TestStreamCapture:
    def test_fast_command_returns_normally(self, tmp_path):
        out, err, rc = CodeImproverAgent._stream_capture(
            args=[sys.executable, "-c", "print('hi')"],
            cwd=str(tmp_path),
            hard_timeout=10.0,
            idle_timeout=5.0,
        )
        assert rc == 0
        assert "hi" in out

    def test_nonzero_exit_is_returned_not_raised(self, tmp_path):
        out, err, rc = CodeImproverAgent._stream_capture(
            args=[sys.executable, "-c", "import sys; sys.exit(7)"],
            cwd=str(tmp_path),
            hard_timeout=10.0,
            idle_timeout=5.0,
        )
        assert rc == 7

    def test_idle_timeout_fires_on_silent_process(self, tmp_path):
        # Sleep 30s producing no output → idle timeout (1s) fires fast.
        # Use a short hard timeout too so a busted idle-detection path
        # can't keep the test running the full 30s.
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            CodeImproverAgent._stream_capture(
                args=[sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(tmp_path),
                hard_timeout=10.0,
                idle_timeout=1.0,
            )
        elapsed = time.monotonic() - started
        # Idle (1s) + poll granularity (0.5s) + cleanup slack — must be
        # well under the 30s sleep, proving idle fired (not hard).
        assert elapsed < 5.0
        assert "idle timeout" in (exc.value.stderr or "")

    def test_progress_resets_idle_timer(self, tmp_path):
        # Emit one line every 0.4s for 2s. With idle=1.0, each emit
        # resets the timer well before it would fire. Hard=10 caps it.
        script = (
            "import sys, time\n"
            "for i in range(5):\n"
            "    print(i, flush=True)\n"
            "    time.sleep(0.4)\n"
        )
        out, err, rc = CodeImproverAgent._stream_capture(
            args=[sys.executable, "-c", script],
            cwd=str(tmp_path),
            hard_timeout=10.0,
            idle_timeout=1.0,
        )
        assert rc == 0
        # All 5 lines made it through despite each gap being >half the
        # idle window — proves the idle timer resets on each emit.
        assert out.splitlines() == ["0", "1", "2", "3", "4"]

    def test_hard_timeout_fires_when_chatty_process_runs_too_long(self, tmp_path):
        # Continuous output keeps idle alive forever; hard timeout
        # must still kill it. Emit every 0.1s for "way longer" than
        # the hard cap.
        script = (
            "import time\n"
            "while True:\n"
            "    print('tick', flush=True)\n"
            "    time.sleep(0.1)\n"
        )
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            CodeImproverAgent._stream_capture(
                args=[sys.executable, "-c", script],
                cwd=str(tmp_path),
                hard_timeout=2.0,
                idle_timeout=10.0,
            )
        elapsed = time.monotonic() - started
        # Should fire close to hard_timeout=2, comfortably under the
        # idle=10 ceiling.
        assert 1.5 <= elapsed < 6.0
        assert "hard timeout" in (exc.value.stderr or "")

    def test_partial_output_survives_timeout(self, tmp_path):
        # When a timeout fires, the partial output collected so far
        # is attached to the exception. Without this, a hang in the
        # middle of pytest leaves the caller with no clue what passed.
        script = (
            "import time\n"
            "print('first', flush=True)\n"
            "print('second', flush=True)\n"
            "time.sleep(30)\n"
        )
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            CodeImproverAgent._stream_capture(
                args=[sys.executable, "-c", script],
                cwd=str(tmp_path),
                hard_timeout=10.0,
                idle_timeout=1.0,
            )
        # Partial stdout MUST contain the lines that made it through.
        assert "first" in (exc.value.output or "")
        assert "second" in (exc.value.output or "")


class TestRunCheckCommandsDefaults:
    """The default cap moved from a flat 180s ``timeout`` to
    ``hard_timeout=1200`` + ``idle_timeout=180``. The default-arg
    behaviour matters: every caller that didn't explicitly pass a
    timeout (the pytest one is the most-trafficked) now gets the
    streaming-idle protection."""

    def test_defaults_use_1200_hard_and_180_idle(self, tmp_path, monkeypatch):
        captured: list[dict] = []

        def fake_stream(*, args, cwd, hard_timeout, idle_timeout):
            captured.append({
                "args": args,
                "hard_timeout": hard_timeout,
                "idle_timeout": idle_timeout,
            })
            return ("", "", 0)

        monkeypatch.setattr(
            CodeImproverAgent, "_stream_capture", staticmethod(fake_stream)
        )
        CodeImproverAgent._run_check_commands(
            tmp_path,
            [(["echo", "hi"], "echo hi")],
        )
        assert captured == [{
            "args": ["echo", "hi"],
            "hard_timeout": 1200,
            "idle_timeout": 180,
        }]

    def test_caller_can_override_idle_timeout(self, tmp_path, monkeypatch):
        captured: list[dict] = []

        def fake_stream(*, args, cwd, hard_timeout, idle_timeout):
            captured.append({"hard_timeout": hard_timeout, "idle_timeout": idle_timeout})
            return ("", "", 0)

        monkeypatch.setattr(
            CodeImproverAgent, "_stream_capture", staticmethod(fake_stream)
        )
        CodeImproverAgent._run_check_commands(
            tmp_path,
            [(["echo", "hi"], "echo hi")],
            timeout=900,
            idle_timeout=60,
        )
        assert captured == [{"hard_timeout": 900, "idle_timeout": 60}]
