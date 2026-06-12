"""Phase 5B — scheduler NL-parse hook tests.

Covers parse_nl_schedule() (richer NL phrases + 'send to <channel>' tail) and
the additive 'schedule_nl' execute() handler that builds a ScheduledJob whose
payload carries delivery routing for the gateway bridge.

These tests use the agent in-process only — no orchestrator, no network, no DB.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from skyn3t.agents.scheduler_agent import NLSchedule, SchedulerAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import Event, EventType

BASE = datetime(2026, 1, 5, 7, 0, 0, tzinfo=timezone.utc)  # Monday 07:00 UTC


# --------------------------------------------------------------------------
# parse_nl_schedule — base-parser reuse paths
# --------------------------------------------------------------------------

def test_parse_nl_reuses_interval_form():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("every 5 minutes", base_time=BASE)
    assert isinstance(nl, NLSchedule)
    assert nl.interval_seconds == 300.0
    assert nl.cron_expr == "every 5 minutes"
    assert nl.next_run is not None
    assert nl.delivery_channel is None
    assert nl.delivery_to is None


def test_parse_nl_reuses_daily_at_form():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("daily at 09:00", base_time=BASE)
    assert nl is not None
    assert nl.cron_expr == "daily at 09:00"
    assert nl.next_run == datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_nl_unknown_returns_none():
    s = SchedulerAgent()
    assert s.parse_nl_schedule("whenever I feel like it", base_time=BASE) is None
    assert s.parse_nl_schedule("", base_time=BASE) is None
    assert s.parse_nl_schedule("   ", base_time=BASE) is None


# --------------------------------------------------------------------------
# parse_nl_schedule — richer NL forms
# --------------------------------------------------------------------------

def test_parse_nl_every_weekday_at_9am():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("every weekday at 9am", base_time=BASE)
    assert nl is not None
    # BASE is Monday 07:00 -> next weekday run is today at 09:00.
    assert nl.next_run == datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert nl.cron_expr == "every weekday at 09:00"
    # weekday cadence is daily-skipping-weekends, not a fixed interval.
    assert nl.interval_seconds is None


def test_parse_nl_every_weekday_skips_to_monday():
    s = SchedulerAgent()
    # Saturday 10:00 -> next weekday run is Monday 09:00.
    sat = datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
    nl = s.parse_nl_schedule("every weekday at 9am", base_time=sat)
    assert nl is not None
    assert nl.next_run == datetime(2026, 1, 12, 9, 0, 0, tzinfo=timezone.utc)
    assert nl.next_run.weekday() == 0  # Monday


def test_parse_nl_daily_briefing_default_time():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("daily briefing at 8", base_time=BASE)
    assert nl is not None
    assert nl.next_run == datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
    assert nl.interval_seconds == 86400.0
    assert nl.cron_expr == "daily at 08:00"


def test_parse_nl_weekly_report_on_monday():
    s = SchedulerAgent()
    # BASE is Monday 07:00, default time 9:00 -> today 09:00.
    nl = s.parse_nl_schedule("weekly report on monday", base_time=BASE)
    assert nl is not None
    assert nl.next_run.weekday() == 0
    assert nl.next_run == datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert nl.interval_seconds == 7 * 86400.0
    assert nl.cron_expr == "weekly on monday at 09:00"


def test_parse_nl_weekly_report_on_friday():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("weekly report on friday at 5pm", base_time=BASE)
    assert nl is not None
    # From Monday, next Friday is 4 days ahead at 17:00.
    assert nl.next_run == datetime(2026, 1, 9, 17, 0, 0, tzinfo=timezone.utc)
    assert nl.next_run.weekday() == 4


# --------------------------------------------------------------------------
# parse_nl_schedule — 'send to <channel>' delivery tail
# --------------------------------------------------------------------------

def test_parse_nl_delivery_tail_channel_only():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("every weekday at 9am send to slack", base_time=BASE)
    assert nl is not None
    assert nl.delivery_channel == "slack"
    assert nl.delivery_to is None
    # The schedule core must still parse correctly after the tail is stripped.
    assert nl.next_run == datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)


def test_parse_nl_delivery_tail_channel_and_target():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("daily at 09:00 send to telegram @ops", base_time=BASE)
    assert nl is not None
    assert nl.delivery_channel == "telegram"
    assert nl.delivery_to == "@ops"
    assert nl.cron_expr == "daily at 09:00"


def test_parse_nl_delivery_teams_alias_normalized():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("daily at 08:00 send to teams", base_time=BASE)
    assert nl is not None
    assert nl.delivery_channel == "msteams"


# --------------------------------------------------------------------------
# execute() schedule_nl handler
# --------------------------------------------------------------------------

def test_execute_schedule_nl_creates_job_with_delivery():
    async def run():
        agent = SchedulerAgent()
        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={
                    "task_type": "schedule_nl",
                    "text": "every weekday at 9am send to slack",
                    "payload": {"agent_name": "reporter", "prompt": "morning briefing"},
                },
            )
        )
        assert result.success is True
        assert result.output["success"] is True
        job_id = result.output["job_id"]
        assert job_id in agent._jobs
        job = agent._jobs[job_id]
        assert job.task_type == "schedule_nl"
        # Payload must carry agent_name/prompt + delivery for the gateway bridge.
        assert job.payload["agent_name"] == "reporter"
        assert job.payload["prompt"] == "morning briefing"
        assert job.payload["delivery"] == {"channel": "slack", "to": None}

    asyncio.run(run())


def test_execute_schedule_nl_explicit_delivery_overrides_tail():
    async def run():
        agent = SchedulerAgent()
        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={
                    "task_type": "schedule_nl",
                    "text": "daily at 09:00 send to slack",
                    "payload": {"agent_name": "reporter", "prompt": "p"},
                    "delivery": {"channel": "telegram", "to": "@team"},
                },
            )
        )
        assert result.output["success"] is True
        job = agent._jobs[result.output["job_id"]]
        # Explicit input_data.delivery wins over the parsed 'send to slack'.
        assert job.payload["delivery"] == {"channel": "telegram", "to": "@team"}

    asyncio.run(run())


def test_execute_schedule_nl_interval_sets_anchor():
    async def run():
        agent = SchedulerAgent()
        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={
                    "task_type": "schedule_nl",
                    "text": "every 10 minutes",
                    "payload": {"agent_name": "a", "prompt": "p"},
                },
            )
        )
        assert result.output["success"] is True
        job = agent._jobs[result.output["job_id"]]
        # Interval forms must carry anchor + interval for the drift-free loop.
        assert job.interval_seconds == 600.0
        assert job.anchor is not None

    asyncio.run(run())


def test_execute_schedule_nl_unparseable_fails_gracefully():
    async def run():
        agent = SchedulerAgent()
        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={
                    "task_type": "schedule_nl",
                    "text": "sometime soon maybe",
                },
            )
        )
        assert result.success is False
        # No job should have been created.
        assert len(agent._jobs) == 0

    asyncio.run(run())


def test_execute_schedule_nl_empty_text_fails():
    async def run():
        agent = SchedulerAgent()
        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={"task_type": "schedule_nl", "text": ""},
            )
        )
        assert result.success is False

    asyncio.run(run())


# --------------------------------------------------------------------------
# _trigger_job carries delivery through the existing SYSTEM_ALERT event shape
# --------------------------------------------------------------------------

def test_trigger_job_emits_delivery_in_existing_event_shape():
    async def run():
        agent = SchedulerAgent()
        captured = []

        def listener(event: Event):
            captured.append(event)

        agent.event_bus.subscribe(listener, EventType.SYSTEM_ALERT)

        result = await agent.execute(
            TaskRequest(
                title="nl",
                input_data={
                    "task_type": "schedule_nl",
                    "text": "daily at 09:00 send to telegram @ops",
                    "payload": {"agent_name": "reporter", "prompt": "p"},
                },
            )
        )
        job = agent._jobs[result.output["job_id"]]
        await agent._trigger_job(job)

        assert len(captured) == 1
        ev = captured[0]
        # The frozen event contract: kind=scheduled_job_triggered, payload=job.payload.
        assert ev.payload["kind"] == "scheduled_job_triggered"
        assert ev.payload["payload"]["delivery"] == {
            "channel": "telegram",
            "to": "@ops",
        }

    asyncio.run(run())


def test_no_delivery_when_no_tail_or_explicit():
    s = SchedulerAgent()
    nl = s.parse_nl_schedule("every 5 minutes", base_time=BASE)
    assert nl is not None
    assert nl.delivery_channel is None
    assert nl.delivery_to is None
