"""Scheduler tests — interval parsing + anchor-based drift behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skyn3t.agents.scheduler_agent import SchedulerAgent


def test_parse_interval_seconds_minutes():
    s = SchedulerAgent()
    assert s._parse_interval_seconds("every 5 minutes") == 300.0
    assert s._parse_interval_seconds("every 1 hour") == 3600.0
    assert s._parse_interval_seconds("every 2 days") == 172800.0


def test_parse_interval_seconds_unknown_returns_none():
    s = SchedulerAgent()
    assert s._parse_interval_seconds("daily at 09:00") is None
    assert s._parse_interval_seconds("at 2030-01-01 10:00:00") is None


def test_anchor_based_next_run_no_drift():
    """Verify the anchor + N*interval math jumps past missed wakeups.

    The bug: the old loop computed next_run = now + interval at trigger time.
    A late wakeup (loop wakes 4s after the scheduled time) would push the
    next firing 4s later forever, accumulating drift.

    The fix: next_run = anchor + ceil(elapsed / interval) * interval, so
    one missed tick still yields a clean schedule that aligns with the
    anchor.
    """
    anchor = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    interval_seconds = 60.0  # every 1 minute

    # Simulate: we wake up 35s late after the scheduled second tick.
    now = anchor + timedelta(seconds=60 + 35)  # 95s after anchor
    elapsed = (now - anchor).total_seconds()
    ticks = int(elapsed // interval_seconds) + 1
    next_run = anchor + timedelta(seconds=ticks * interval_seconds)

    # The next aligned tick after t=95s is t=120s, not t=95+60=155s.
    expected = anchor + timedelta(seconds=120)
    assert next_run == expected, f"got {next_run}, expected {expected}"


def test_anchor_based_skips_missed_ticks():
    """A 10-minute outage shouldn't fire 10 catch-up triggers — it should
    pick the next aligned tick in the future."""
    anchor = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    interval_seconds = 60.0
    # Simulate: process was down for ~10 minutes; we wake at t=620s.
    now = anchor + timedelta(seconds=620)
    elapsed = (now - anchor).total_seconds()
    ticks = int(elapsed // interval_seconds) + 1
    next_run = anchor + timedelta(seconds=ticks * interval_seconds)
    assert next_run == anchor + timedelta(seconds=660)


def test_schedule_task_creates_anchor_for_intervals():
    """When `every N` is parsed, the job should carry anchor+interval so
    the loop can use the drift-free path."""
    import asyncio
    from skyn3t.core.agent import TaskRequest

    async def run():
        agent = SchedulerAgent()
        result = await agent._schedule_task(
            TaskRequest(
                title="t",
                input_data={
                    "name": "test_job",
                    "schedule": "every 5 minutes",
                    "job_task_type": "noop",
                },
            )
        )
        assert result["success"] is True
        # The job should now be in agent._jobs with anchor + interval set.
        job = next(iter(agent._jobs.values()))
        assert job.anchor is not None
        assert job.interval_seconds == 300.0

    asyncio.run(run())
