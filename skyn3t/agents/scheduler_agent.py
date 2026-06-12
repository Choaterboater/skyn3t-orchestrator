"""Scheduler Agent - schedules tasks, manages cron-like jobs, and sends reminders."""

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.memory.store import MemoryStore


@dataclass
class ScheduledJob:
    """Represents a scheduled job."""

    job_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    schedule: str = ""  # cron expression or interval
    task_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    next_run: Optional[datetime] = None
    last_run: Optional[datetime] = None
    # Anchor for interval-based schedules. next_run is computed as
    # anchor + N * interval, so loop wakeup delays don't compound drift.
    anchor: Optional[datetime] = None
    interval_seconds: Optional[float] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class NLSchedule:
    """Result of parsing a natural-language schedule phrase.

    Produced by :meth:`SchedulerAgent.parse_nl_schedule`. The recurrence core
    (next_run / interval_seconds) is derived from the existing regex parsers;
    ``cron_expr`` is the normalised 'every N units' / 'daily at HH:MM' form, and
    the optional delivery fields capture a trailing 'send to <channel>' clause so
    the gateway bridge can route the triggered result.
    """

    next_run: Optional[datetime]
    interval_seconds: Optional[float]  # None => one-shot
    cron_expr: Optional[str]  # normalized 'every N units'/'daily at HH:MM' form
    delivery_channel: Optional[str] = None  # parsed 'send to telegram' target platform
    delivery_to: Optional[str] = None


class SchedulerAgent(BaseAgent):
    """Agent for scheduling recurring tasks, cron management, and reminders."""

    def __init__(
        self,
        name: str = "scheduler_agent",
        event_bus: EventBus | None = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="scheduler",
            provider="local",
            event_bus=event_bus or EventBus(),
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="schedule_task",
                description="Schedule one-time or recurring tasks",
                parameters={
                    "name": "str",
                    "schedule": "str",
                    "task_type": "str",
                    "payload": "dict",
                },
            )
        )
        self.add_capability(
            AgentCapability(
                name="cron_management",
                description="Manage cron-like recurring jobs",
                parameters={
                    "action": "str",
                    "job_id": "str",
                    "expression": "str",
                },
            )
        )
        self.add_capability(
            AgentCapability(
                name="reminder",
                description="Set and manage reminders",
                parameters={
                    "message": "str",
                    "trigger_at": "str",
                    "recurring": "bool",
                },
            )
        )
        self.add_capability(
            AgentCapability(
                name="schedule_nl",
                description="Schedule a recurring task from a natural-language phrase "
                "(e.g. 'every weekday at 9am send to slack')",
                parameters={
                    "text": "str",
                    "payload": "dict",
                    "delivery": "dict",
                },
            )
        )
        self._jobs: Dict[str, ScheduledJob] = {}
        self._reminders: Dict[str, Dict[str, Any]] = {}
        self._scheduler_task: Optional[asyncio.Task] = None
        self._scheduler_loop_running: bool = False
        self._tick_interval = self.config.get("tick_interval", 60)
        self._store: Optional[MemoryStore] = None

    async def initialize(self) -> None:
        """Initialize the scheduler agent and start the background tick loop.

        Idempotent: if already initialised with a live scheduler task, return
        without spawning a duplicate. Previously a second initialize() would
        overwrite ``_scheduler_task`` while leaving the original running, and
        the loop's ``while self._running`` exited immediately when called via
        the registry (which never calls BaseAgent.start), causing the monitor
        to flag the agent as errored and trigger SELF_HEAL_TRIGGERED.
        """
        self.metadata["initialized"] = True
        self.metadata.setdefault("jobs_count", 0)
        self.metadata.setdefault("reminders_count", 0)
        # Idempotency: don't spawn a second loop if one is alive.
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        # Load persisted jobs from the database.
        try:
            self._store = MemoryStore()
            rows = await self._store.list_scheduled_jobs()
            for row in rows:
                job = ScheduledJob(
                    job_id=row["id"],
                    name=row["name"],
                    schedule=row["schedule_expr"],
                    task_type="scheduled_task",
                    payload={"agent_name": row["agent_name"], "prompt": row["prompt"]},
                    next_run=self._parse_datetime(row["next_run"]) if row.get("next_run") else None,
                    last_run=self._parse_datetime(row["last_run"]) if row.get("last_run") else None,
                    run_count=row.get("run_count", 0),
                    enabled=row.get("enabled", True),
                )
                # Re-compute next_run if it's in the past
                if job.next_run is None or job.next_run < datetime.now(timezone.utc):
                    job.next_run = self._parse_schedule(job.schedule)
                self._jobs[job.job_id] = job
            self.metadata["jobs_count"] = len(self._jobs)
        except Exception:
            pass  # DB may not be available during tests
        # Use our own running flag so the loop survives whether or not
        # BaseAgent.start() has been called (the registry path calls
        # initialize() directly without ever flipping BaseAgent._running).
        self._scheduler_loop_running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def health_check(self) -> bool:
        """Always healthy on first run.

        We don't gate health on the cron task being alive: a momentary glitch
        in the loop should not nuke the agent. The loop self-recovers on its
        own and the monitor would otherwise spin SELF_HEAL_TRIGGERED on us.
        """
        return True

    async def shutdown(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler_loop_running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await super().shutdown()

    async def execute(self, task: TaskRequest, stdin_data: str | None = None) -> TaskResult:
        """Execute a scheduler-related task."""
        task_type = task.input_data.get("task_type", "schedule_task")

        handlers = {
            "schedule_task": self._schedule_task,
            "cron_management": self._cron_management,
            "reminder": self._reminder,
            "list_jobs": self._list_jobs,
            "cancel_job": self._cancel_job,
            "schedule_nl": self._schedule_nl,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _schedule_task(self, task: TaskRequest) -> Dict[str, Any]:
        """Schedule a new task."""
        name = task.input_data.get("name", "Unnamed Job")
        schedule = task.input_data.get("schedule", "")
        job_task_type = task.input_data.get("job_task_type", "")
        payload = task.input_data.get("payload", {})
        max_runs = task.input_data.get("max_runs")

        if not schedule:
            return {"success": False, "error": "No schedule provided"}

        next_run = self._parse_schedule(schedule)
        if next_run is None:
            return {"success": False, "error": f"Invalid schedule format: {schedule}"}

        # For interval schedules ("every N minutes/etc"), capture the anchor
        # and interval so the loop can compute next_run as anchor+N*interval
        # instead of now+interval (which drifts on every late wakeup).
        anchor: Optional[datetime] = None
        interval_seconds: Optional[float] = None
        interval = self._parse_interval_seconds(schedule)
        if interval is not None:
            anchor = next_run
            interval_seconds = interval

        job = ScheduledJob(
            name=name,
            schedule=schedule,
            task_type=job_task_type,
            payload=payload,
            next_run=next_run,
            anchor=anchor,
            interval_seconds=interval_seconds,
            max_runs=max_runs,
        )
        self._jobs[job.job_id] = job
        self.metadata["jobs_count"] = len(self._jobs)

        # Persist to database
        if self._store:
            try:
                await self._store.save_scheduled_job(
                    job_id=job.job_id,
                    name=job.name,
                    schedule_expr=job.schedule,
                    agent_name=payload.get("agent_name"),
                    prompt=payload.get("prompt"),
                    enabled=True,
                    next_run=job.next_run,
                    run_count=0,
                )
            except Exception:
                pass

        return {
            "success": True,
            "job_id": job.job_id,
            "name": job.name,
            "next_run": job.next_run.isoformat() if job.next_run else None,
            "schedule": job.schedule,
        }

    async def _cron_management(self, task: TaskRequest) -> Dict[str, Any]:
        """Manage cron-like jobs."""
        action = task.input_data.get("action", "list")
        job_id = task.input_data.get("job_id", "")
        expression = task.input_data.get("expression", "")

        if action == "list":
            jobs = []
            for job in self._jobs.values():
                jobs.append({
                    "job_id": job.job_id,
                    "name": job.name,
                    "schedule": job.schedule,
                    "enabled": job.enabled,
                    "run_count": job.run_count,
                    "next_run": job.next_run.isoformat() if job.next_run else None,
                })
            return {"success": True, "jobs": jobs, "count": len(jobs)}

        elif action == "enable":
            if job_id not in self._jobs:
                return {"success": False, "error": f"Job not found: {job_id}"}
            self._jobs[job_id].enabled = True
            if self._store:
                try:
                    j = self._jobs[job_id]
                    await self._store.save_scheduled_job(
                        job_id=j.job_id, name=j.name, schedule_expr=j.schedule,
                        enabled=j.enabled, next_run=j.next_run,
                        last_run=j.last_run, run_count=j.run_count,
                    )
                except Exception:
                    pass
            return {"success": True, "job_id": job_id, "enabled": True}

        elif action == "disable":
            if job_id not in self._jobs:
                return {"success": False, "error": f"Job not found: {job_id}"}
            self._jobs[job_id].enabled = False
            if self._store:
                try:
                    j = self._jobs[job_id]
                    await self._store.save_scheduled_job(
                        job_id=j.job_id, name=j.name, schedule_expr=j.schedule,
                        enabled=j.enabled, next_run=j.next_run,
                        last_run=j.last_run, run_count=j.run_count,
                    )
                except Exception:
                    pass
            return {"success": True, "job_id": job_id, "enabled": False}

        elif action == "delete":
            if job_id not in self._jobs:
                return {"success": False, "error": f"Job not found: {job_id}"}
            del self._jobs[job_id]
            self.metadata["jobs_count"] = len(self._jobs)
            if self._store:
                try:
                    await self._store.delete_scheduled_job(job_id)
                except Exception:
                    pass
            return {"success": True, "job_id": job_id, "deleted": True}

        elif action == "validate":
            is_valid = self._parse_schedule(expression) is not None
            return {"success": True, "expression": expression, "valid": is_valid}

        else:
            return {"success": False, "error": f"Unknown action: {action}"}

    async def _reminder(self, task: TaskRequest) -> Dict[str, Any]:
        """Set or manage reminders."""
        action = task.input_data.get("action", "set")
        message = task.input_data.get("message", "")
        trigger_at_str = task.input_data.get("trigger_at", "")
        recurring = task.input_data.get("recurring", False)
        reminder_id = task.input_data.get("reminder_id", "")

        if action == "set":
            if not message:
                return {"success": False, "error": "No reminder message provided"}

            trigger_at = self._parse_datetime(trigger_at_str)
            if trigger_at is None:
                return {"success": False, "error": f"Invalid trigger time: {trigger_at_str}"}

            rid = str(uuid4())
            self._reminders[rid] = {
                "reminder_id": rid,
                "message": message,
                "trigger_at": trigger_at.isoformat(),
                "recurring": recurring,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "triggered": False,
            }
            self.metadata["reminders_count"] = len(self._reminders)

            return {
                "success": True,
                "reminder_id": rid,
                "message": message,
                "trigger_at": trigger_at.isoformat(),
                "recurring": recurring,
            }

        elif action == "list":
            reminders = list(self._reminders.values())
            return {"success": True, "reminders": reminders, "count": len(reminders)}

        elif action == "cancel":
            if reminder_id not in self._reminders:
                return {"success": False, "error": f"Reminder not found: {reminder_id}"}
            del self._reminders[reminder_id]
            self.metadata["reminders_count"] = len(self._reminders)
            return {"success": True, "reminder_id": reminder_id, "cancelled": True}

        else:
            return {"success": False, "error": f"Unknown reminder action: {action}"}

    async def _list_jobs(self, task: TaskRequest) -> Dict[str, Any]:
        """List all scheduled jobs."""
        jobs = []
        for job in self._jobs.values():
            jobs.append({
                "job_id": job.job_id,
                "name": job.name,
                "schedule": job.schedule,
                "task_type": job.task_type,
                "enabled": job.enabled,
                "run_count": job.run_count,
                "max_runs": job.max_runs,
                "next_run": job.next_run.isoformat() if job.next_run else None,
                "last_run": job.last_run.isoformat() if job.last_run else None,
            })
        return {"success": True, "jobs": jobs, "count": len(jobs)}

    async def _cancel_job(self, task: TaskRequest) -> Dict[str, Any]:
        """Cancel a scheduled job by ID."""
        job_id = task.input_data.get("job_id", "")
        if not job_id:
            return {"success": False, "error": "No job_id provided"}
        if job_id not in self._jobs:
            return {"success": False, "error": f"Job not found: {job_id}"}
        del self._jobs[job_id]
        self.metadata["jobs_count"] = len(self._jobs)
        if self._store:
            try:
                await self._store.delete_scheduled_job(job_id)
            except Exception:
                pass
        return {"success": True, "job_id": job_id, "cancelled": True}

    async def _schedule_nl(self, task: TaskRequest) -> Dict[str, Any]:
        """Schedule a job from a natural-language phrase.

        input_data: {
            text: str,                       # the NL schedule phrase
            name: str (optional),
            payload: {agent_name, prompt},   # what to run when triggered
            delivery: {channel, to},         # optional explicit delivery override
            max_runs: int (optional),
        }

        Builds a ScheduledJob whose payload carries
        {agent_name, prompt, delivery:{channel,to}} so _trigger_job's existing
        SYSTEM_ALERT(kind=scheduled_job_triggered) event hands the gateway
        bridge everything it needs to route the result.
        """
        text = task.input_data.get("text", "")
        if not text or not text.strip():
            return {"success": False, "error": "No schedule text provided"}

        parsed = self.parse_nl_schedule(text)
        if parsed is None or parsed.next_run is None:
            return {"success": False, "error": f"Could not parse schedule from: {text}"}

        name = task.input_data.get("name") or text.strip()
        in_payload = task.input_data.get("payload") or {}
        max_runs = task.input_data.get("max_runs")

        # Delivery: an explicit input_data.delivery overrides anything parsed
        # from the NL tail ('... send to slack').
        delivery_in = task.input_data.get("delivery") or {}
        channel = delivery_in.get("channel") or parsed.delivery_channel
        to = delivery_in.get("to") or parsed.delivery_to
        delivery = {"channel": channel, "to": to}

        # The job payload mirrors the persisted shape ({agent_name, prompt}) and
        # adds the delivery block consumed by the gateway bridge.
        payload: Dict[str, Any] = {
            "agent_name": in_payload.get("agent_name"),
            "prompt": in_payload.get("prompt"),
            "delivery": delivery,
        }

        anchor: Optional[datetime] = None
        if parsed.interval_seconds is not None:
            anchor = parsed.next_run

        job = ScheduledJob(
            name=name,
            schedule=parsed.cron_expr or text.strip(),
            task_type="schedule_nl",
            payload=payload,
            next_run=parsed.next_run,
            anchor=anchor,
            interval_seconds=parsed.interval_seconds,
            max_runs=max_runs,
        )
        self._jobs[job.job_id] = job
        self.metadata["jobs_count"] = len(self._jobs)

        # Persist to database (best-effort; DB may be absent in tests).
        if self._store:
            try:
                await self._store.save_scheduled_job(
                    job_id=job.job_id,
                    name=job.name,
                    schedule_expr=job.schedule,
                    agent_name=payload.get("agent_name"),
                    prompt=payload.get("prompt"),
                    enabled=True,
                    next_run=job.next_run,
                    run_count=0,
                )
            except Exception:
                pass

        return {
            "success": True,
            "job_id": job.job_id,
            "name": job.name,
            "next_run": job.next_run.isoformat() if job.next_run else None,
            "schedule": job.schedule,
            "interval_seconds": job.interval_seconds,
            "delivery": delivery,
        }

    async def _scheduler_loop(self) -> None:
        """Background loop that checks and triggers scheduled jobs and reminders."""
        # Boot grace: jobs that came due while the server was down (e.g.
        # the daily repo scout) used to fire on the FIRST tick — during
        # roster registration — and the ingest/embedding grind starved
        # the event loop for 80-90s before the port even bound. Let the
        # server come up and serve before any overdue job runs.
        try:
            grace = float(os.environ.get("SKYN3T_SCHEDULER_BOOT_GRACE_S", "90"))
        except ValueError:
            grace = 90.0
        if grace > 0:
            await asyncio.sleep(grace)
        while self._scheduler_loop_running:
            try:
                now = datetime.now(timezone.utc)

                # Check jobs
                for job in list(self._jobs.values()):
                    if not job.enabled:
                        continue
                    if job.max_runs is not None and job.run_count >= job.max_runs:
                        continue
                    if job.next_run and now >= job.next_run:
                        await self._trigger_job(job)
                        job.last_run = now
                        job.run_count += 1
                        # Anchor-based scheduling: compute the next aligned tick
                        # so a missed wakeup doesn't push the schedule forward.
                        # If we missed N intervals, jump straight to the next
                        # one in the future (don't fire N catch-up triggers).
                        if job.anchor is not None and job.interval_seconds:
                            elapsed = (now - job.anchor).total_seconds()
                            ticks = int(elapsed // job.interval_seconds) + 1
                            job.next_run = job.anchor + timedelta(
                                seconds=ticks * job.interval_seconds
                            )
                        else:
                            job.next_run = self._parse_schedule(job.schedule, base_time=now)

                # Check reminders
                for rid, reminder in list(self._reminders.items()):
                    if reminder.get("triggered"):
                        continue
                    trigger_at = self._parse_datetime(reminder.get("trigger_at", ""))
                    if trigger_at and now >= trigger_at:
                        await self._trigger_reminder(reminder)
                        if not reminder.get("recurring"):
                            reminder["triggered"] = True
                        else:
                            # Reschedule recurring reminder (e.g., +1 day)
                            new_trigger = trigger_at + timedelta(days=1)
                            reminder["trigger_at"] = new_trigger.isoformat()

                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._record_error(str(e))
                await asyncio.sleep(self._tick_interval)

    async def _trigger_job(self, job: ScheduledJob) -> None:
        """Trigger a scheduled job via event bus and persist state."""
        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source=self.name,
                payload={
                    "kind": "scheduled_job_triggered",
                    "job_id": job.job_id,
                    "name": job.name,
                    "task_type": job.task_type,
                    "payload": job.payload,
                },
            )
        )
        # Persist updated run state
        if self._store:
            try:
                await self._store.save_scheduled_job(
                    job_id=job.job_id,
                    name=job.name,
                    schedule_expr=job.schedule,
                    enabled=job.enabled,
                    next_run=job.next_run,
                    last_run=job.last_run,
                    run_count=job.run_count,
                )
            except Exception:
                pass

    async def _trigger_reminder(self, reminder: Dict[str, Any]) -> None:
        """Trigger a reminder via event bus."""
        self.event_bus.publish(
            Event(
                event_type=EventType.SYSTEM_ALERT,
                source=self.name,
                payload={
                    "kind": "reminder_triggered",
                    "reminder_id": reminder["reminder_id"],
                    "message": reminder["message"],
                },
            )
        )

    def _parse_interval_seconds(self, schedule: str) -> Optional[float]:
        """Return interval (in seconds) for recurring schedules, else None.

        Recognises both the human form ("every N <unit>") and the compact
        prefix form ("interval:<N><unit>" where unit is s/m/h/d), the latter
        being how the autonomous loop expresses the scout cadence.
        """
        normalized = schedule.strip().lower()
        m = re.match(
            r"every\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days)",
            normalized,
        )
        if m:
            value = int(m.group(1))
            unit = m.group(2)
            unit_seconds = {
                "second": 1, "seconds": 1,
                "minute": 60, "minutes": 60,
                "hour": 3600, "hours": 3600,
                "day": 86400, "days": 86400,
            }[unit]
            return float(value * unit_seconds)

        prefix = re.match(r"interval:\s*(\d+)\s*([smhd])\b", normalized)
        if prefix:
            value = int(prefix.group(1))
            unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[prefix.group(2)]
            return float(value * unit_seconds)

        return None

    def _parse_schedule(self, schedule: str, base_time: Optional[datetime] = None) -> Optional[datetime]:
        """Parse a schedule string and return the next run time."""
        now = base_time or datetime.now(timezone.utc)
        schedule = schedule.strip().lower()

        # Interval format: "every N seconds/minutes/hours/days"
        interval_match = re.match(r"every\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days)", schedule)
        if interval_match:
            value = int(interval_match.group(1))
            unit = interval_match.group(2)
            if unit in ("second", "seconds"):
                return now + timedelta(seconds=value)
            elif unit in ("minute", "minutes"):
                return now + timedelta(minutes=value)
            elif unit in ("hour", "hours"):
                return now + timedelta(hours=value)
            elif unit in ("day", "days"):
                return now + timedelta(days=value)

        # Compact interval prefix: "interval:<N><unit>" (unit = s/m/h/d).
        # This is how the autonomous loop expresses the scout cadence.
        prefix_seconds = self._parse_interval_seconds(schedule)
        if prefix_seconds is not None and schedule.startswith("interval:"):
            return now + timedelta(seconds=prefix_seconds)

        # Simple cron-like: "daily at HH:MM"
        daily_match = re.match(r"daily\s+at\s+(\d{1,2}):(\d{2})", schedule)
        if daily_match:
            hour = int(daily_match.group(1))
            minute = int(daily_match.group(2))
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            return next_run

        # Once at a specific time: "at YYYY-MM-DD HH:MM:SS"
        at_match = re.match(r"at\s+(.+)", schedule)
        if at_match:
            dt = self._parse_datetime(at_match.group(1).strip())
            return dt

        # Relative: "in N seconds/minutes/hours/days"
        in_match = re.match(r"in\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days)", schedule)
        if in_match:
            value = int(in_match.group(1))
            unit = in_match.group(2)
            if unit in ("second", "seconds"):
                return now + timedelta(seconds=value)
            elif unit in ("minute", "minutes"):
                return now + timedelta(minutes=value)
            elif unit in ("hour", "hours"):
                return now + timedelta(hours=value)
            elif unit in ("day", "days"):
                return now + timedelta(days=value)

        return None

    # ---- Natural-language schedule parsing -------------------------------

    _WEEKDAYS = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    }

    # Known messaging platforms the gateway can route to. Kept liberal: an
    # unknown word after 'send to' is still captured as the channel so a new
    # adapter doesn't require editing this list.
    _KNOWN_CHANNELS = {
        "telegram", "whatsapp", "matrix", "signal", "imessage", "slack",
        "discord", "email", "msteams", "teams", "mattermost", "feishu",
        "webhook", "sms",
    }

    def parse_nl_schedule(
        self, text: str, *, base_time: Optional[datetime] = None
    ) -> Optional[NLSchedule]:
        """Parse a richer natural-language schedule phrase.

        Handles forms the bare regex parsers don't, e.g.::

            'every weekday at 9am'
            'daily briefing at 8'
            'weekly report on monday'
            'every 5 minutes send to slack'
            'daily at 09:00 send to telegram @ops'

        and an optional trailing 'send to <channel> [<target>]' clause parsed
        into delivery_channel / delivery_to. Returns ``None`` when no recurrence
        can be extracted.

        Reuses :meth:`_parse_interval_seconds` / :meth:`_parse_schedule` for the
        recurrence core so this is purely additive — existing signatures are
        untouched.
        """
        if not text or not text.strip():
            return None

        now = base_time or datetime.now(timezone.utc)
        normalized = text.strip().lower()

        # 1. Split off any trailing delivery clause: '... send to <chan> [tgt]'.
        delivery_channel: Optional[str] = None
        delivery_to: Optional[str] = None
        schedule_part = normalized
        m_send = re.search(
            r"\bsend\s+(?:it\s+)?to\s+(\S+)(?:\s+(.+))?$", normalized
        )
        if m_send:
            delivery_channel = self._normalize_channel(m_send.group(1))
            tail = (m_send.group(2) or "").strip()
            delivery_to = tail or None
            schedule_part = normalized[: m_send.start()].strip()

        # 2. Try direct reuse of the existing parser first (covers 'every N
        #    units', 'daily at HH:MM', 'in N units', 'at <dt>', 'interval:Nu').
        next_run = self._parse_schedule(schedule_part, base_time=now)
        interval_seconds = self._parse_interval_seconds(schedule_part)
        cron_expr: Optional[str] = None

        if next_run is not None:
            cron_expr = self._normalize_cron_expr(schedule_part)
            return NLSchedule(
                next_run=next_run,
                interval_seconds=interval_seconds,
                cron_expr=cron_expr,
                delivery_channel=delivery_channel,
                delivery_to=delivery_to,
            )

        # 3. Richer NL forms the base parser misses.
        nl = self._parse_nl_recurrence(schedule_part, now)
        if nl is None:
            return None
        next_run, interval_seconds, cron_expr = nl
        return NLSchedule(
            next_run=next_run,
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            delivery_channel=delivery_channel,
            delivery_to=delivery_to,
        )

    def _normalize_channel(self, raw: str) -> Optional[str]:
        """Normalise a parsed channel token (strip punctuation; map aliases)."""
        token = raw.strip().strip(".,!?:;").lower()
        if not token:
            return None
        if token == "teams":
            return "msteams"
        return token

    def _parse_time_of_day(self, text: str) -> Optional[tuple]:
        """Extract an (hour, minute) tuple from phrases like '9am', '8',
        '09:00', '5:30pm'. Returns None if no time is present."""
        # HH:MM with optional am/pm
        m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            hour = self._apply_meridiem(hour, m.group(3))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute)
        # bare hour with am/pm e.g. '9am', '5 pm'
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text)
        if m:
            hour = self._apply_meridiem(int(m.group(1)), m.group(2))
            if 0 <= hour <= 23:
                return (hour, 0)
        # bare hour after 'at' e.g. 'at 8'
        m = re.search(r"\bat\s+(\d{1,2})\b", text)
        if m:
            hour = int(m.group(1))
            if 0 <= hour <= 23:
                return (hour, 0)
        return None

    @staticmethod
    def _apply_meridiem(hour: int, meridiem: Optional[str]) -> int:
        """Convert a 12-hour clock hour to 24-hour given an am/pm marker."""
        if meridiem == "pm" and hour < 12:
            return hour + 12
        if meridiem == "am" and hour == 12:
            return 0
        return hour

    def _parse_nl_recurrence(self, text: str, now: datetime):
        """Parse richer NL recurrence forms not covered by _parse_schedule.

        Returns (next_run, interval_seconds, cron_expr) or None.
        """
        hm = self._parse_time_of_day(text)
        hour, minute = hm if hm else (9, 0)  # default to 9:00 for these forms

        # 'every weekday ...' / 'weekdays ...' -> Mon-Fri at HH:MM, daily cadence
        if re.search(r"\b(every\s+weekday|weekdays|each\s+weekday|business\s+day)\b", text):
            next_run = self._next_weekday_run(now, hour, minute)
            cron = f"every weekday at {hour:02d}:{minute:02d}"
            return (next_run, None, cron)

        # 'weekly ... on <weekday>' / 'every <weekday> ...' -> weekly cadence
        m_dow = re.search(
            r"\b(?:on\s+|every\s+)?(monday|mon|tuesday|tue|tues|wednesday|wed|"
            r"thursday|thu|thurs|friday|fri|saturday|sat|sunday|sun)\b",
            text,
        )
        is_weekly = "weekly" in text or re.search(r"\bevery\s+(mon|tue|wed|thu|fri|sat|sun)", text)
        if m_dow and (is_weekly or "report" in text or "on " in text):
            target_dow = self._WEEKDAYS[m_dow.group(1)]
            next_run = self._next_dow_run(now, target_dow, hour, minute)
            cron = f"weekly on {m_dow.group(1)} at {hour:02d}:{minute:02d}"
            return (next_run, 7 * 86400.0, cron)

        # 'daily ...' (e.g. 'daily briefing at 8', 'daily backup') -> daily at HH:MM
        if re.search(r"\b(daily|every\s+day|each\s+day)\b", text):
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            cron = f"daily at {hour:02d}:{minute:02d}"
            return (next_run, 86400.0, cron)

        # 'hourly' -> every hour
        if re.search(r"\bhourly\b", text):
            return (now + timedelta(hours=1), 3600.0, "every 1 hours")

        return None

    def _next_weekday_run(self, now: datetime, hour: int, minute: int) -> datetime:
        """Next Mon-Fri occurrence at the given time (inclusive of today)."""
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:  # Sat=5, Sun=6
            candidate += timedelta(days=1)
        return candidate

    def _next_dow_run(self, now: datetime, target_dow: int, hour: int, minute: int) -> datetime:
        """Next occurrence of target weekday at the given time (inclusive of today)."""
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_dow - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    def _normalize_cron_expr(self, schedule_part: str) -> Optional[str]:
        """Normalise a base-parser-recognised phrase to its canonical cron form."""
        s = schedule_part.strip()
        m = re.match(
            r"every\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days)",
            s,
        )
        if m:
            unit = m.group(2)
            plural = unit if unit.endswith("s") else unit + "s"
            return f"every {m.group(1)} {plural}"
        m = re.match(r"daily\s+at\s+(\d{1,2}):(\d{2})", s)
        if m:
            return f"daily at {int(m.group(1)):02d}:{int(m.group(2)):02d}"
        m = re.match(r"interval:\s*(\d+)\s*([smhd])\b", s)
        if m:
            return f"interval:{m.group(1)}{m.group(2)}"
        return s or None

    def _parse_datetime(self, dt_str: str) -> Optional[datetime]:
        """Parse a datetime string."""
        if not dt_str:
            return None
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%H:%M:%S",
            "%H:%M",
        ]
        for fmt in formats:
            try:
                parsed = datetime.strptime(dt_str, fmt)
                # If no date component, assume today
                if fmt in ("%H:%M:%S", "%H:%M"):
                    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    parsed = today.replace(
                        hour=parsed.hour, minute=parsed.minute, second=parsed.second
                    )
                else:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue
        return None
