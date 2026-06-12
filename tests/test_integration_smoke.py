"""End-to-end integration smoke tests.

These tests exercise the full orchestrator + agent + event-bus stack
(no mocks of the orchestrator itself) so they catch regressions in the
wiring between components — things unit tests with mocks can miss.
"""

import threading
import time
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi.testclient import TestClient

import skyn3t.web.app as web_app
from skyn3t.core.agent import BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.persistence.checkpoint import Checkpoint, CheckpointManager

# ---------------------------------------------------------------------------
# Minimal test agent
# ---------------------------------------------------------------------------


class SmokeAgent(BaseAgent):
    """Minimal real BaseAgent subclass used by these integration tests."""

    def __init__(
        self,
        name: str,
        event_bus: EventBus,
        *,
        fail: bool = False,
        raise_exc: bool = False,
    ):
        super().__init__(
            name=name,
            agent_type="smoke",
            provider="test",
            event_bus=event_bus,
        )
        self._fail = fail
        self._raise_exc = raise_exc
        self.executed_task_ids: list[str] = []

    async def initialize(self) -> None:
        self.status = "idle"

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest, stdin_data: Optional[str] = None) -> TaskResult:
        self.executed_task_ids.append(task.task_id)
        if self._raise_exc:
            raise RuntimeError("intentional execute failure")
        if self._fail:
            return TaskResult(task_id=task.task_id, success=False, error="forced failure")
        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"echo": task.title, "stdout": task.title},
        )


@pytest.fixture
def smoke_orchestrator():
    """Build an Orchestrator with no cortex/memory side-effects.

    We do NOT call orch.start() here because that triggers cortex + the
    default-roster registration which we don't want in tight unit-style
    smoke tests. Tests that need start() call it themselves.
    """
    bus = EventBus()
    orch = Orchestrator(bus)
    return orch


# ---------------------------------------------------------------------------
# 1. Boot + register + submit + wait_for_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_boot_register_submit_and_wait():
    bus = EventBus()
    orch = Orchestrator(bus)
    agent = SmokeAgent("smoke1", bus)
    await agent.start()
    orch.register_agent(agent)
    # Skip orch.start() to avoid cortex + default roster side-effects;
    # submit_task itself doesn't require orch._running.

    task = TaskRequest(title="hello", description="say hi")
    task_id = await orch.submit_task(task, agent_name="smoke1")
    assert task_id == task.task_id

    result = await orch.wait_for_task(task_id, timeout=5.0)

    try:
        assert result is not None, "wait_for_task timed out"
        assert result.success is True
        assert task_id in orch.task_results
        assert orch.task_results[task_id].output.get("echo") == "hello"
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# 2. Idempotency key dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_returns_same_task_id():
    bus = EventBus()
    orch = Orchestrator(bus)
    agent = SmokeAgent("smoke2", bus)
    await agent.start()
    orch.register_agent(agent)

    try:
        t1 = TaskRequest(title="one", idempotency_key="dedup-key-A")
        first_id = await orch.submit_task(t1, agent_name="smoke2")

        t2 = TaskRequest(title="two", idempotency_key="dedup-key-A")
        second_id = await orch.submit_task(t2, agent_name="smoke2")

        assert first_id == second_id
        # The second TaskRequest must NOT have been delivered to the agent.
        # Give the agent a little time to drain its queue, then check.
        await orch.wait_for_task(first_id, timeout=5.0)
        assert t2.task_id not in agent.executed_task_ids
        assert t1.task_id in agent.executed_task_ids
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# 3. Queue backpressure rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_backpressure_reject_when_full(monkeypatch):
    bus = EventBus()
    orch = Orchestrator(bus)

    # Force max_queue_depth=2 via the settings hook BaseAgent reads when
    # lazily creating its queue.
    from skyn3t.core import agent as agent_mod

    fake_settings = SimpleNamespace(max_queue_depth=2)
    monkeypatch.setattr(
        agent_mod, "get_settings", lambda: fake_settings, raising=False
    )
    # The import inside _task_queue is `from skyn3t.config.settings import get_settings`,
    # so we also need to patch the source.
    import skyn3t.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake_settings)

    # Build the agent BEFORE start so the queue is created lazily with maxsize=2.
    agent = SmokeAgent("smoke3", bus)
    # Don't start the processor — we want tasks to pile up in the queue and
    # not be drained, so the third submission triggers backpressure reject.
    # Force queue creation now (still maxsize=2 thanks to the patched setting).
    _ = agent._task_queue
    assert agent._task_queue.maxsize == 2

    rejects: list[Event] = []
    bus.subscribe(lambda ev: rejects.append(ev), EventType.QUEUE_BACKPRESSURE_REJECT)

    orch.register_agent(agent)

    # Two tasks fill the queue.
    await agent.submit_task(TaskRequest(title="t1"))
    await agent.submit_task(TaskRequest(title="t2"))

    # The third must be rejected.
    rejected_task = TaskRequest(title="t3-rejected")
    await agent.submit_task(rejected_task)

    assert len(rejects) >= 1
    # Find an event matching our rejected task.
    matching = [ev for ev in rejects if ev.payload.get("task_id") == rejected_task.task_id]
    assert matching, f"expected QUEUE_BACKPRESSURE_REJECT for {rejected_task.task_id}"
    assert matching[0].payload["queue_max"] == 2


# ---------------------------------------------------------------------------
# 4. Task handler raises -> TASK_FAILED is published, retried or finalized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_failure_publishes_event_and_resolves():
    bus = EventBus()
    orch = Orchestrator(bus)
    agent = SmokeAgent("smoke4", bus, raise_exc=True)
    await agent.start()
    orch.register_agent(agent)

    failed_events: list[Event] = []
    final_events: list[Event] = []
    bus.subscribe(lambda ev: failed_events.append(ev), EventType.TASK_FAILED)
    bus.subscribe(lambda ev: final_events.append(ev), EventType.TASK_FAILED_FINAL)

    try:
        # max_retries=0 so the orchestrator immediately finalizes the failure
        # rather than entering the (slow) exponential-backoff retry path.
        task = TaskRequest(title="will_fail", max_retries=0)
        task_id = await orch.submit_task(task, agent_name="smoke4")
        result = await orch.wait_for_task(task_id, timeout=5.0)

        assert result is not None
        # Task must end up either failed-final OR retried; either way TASK_FAILED
        # must have been published, and the orchestrator must have a terminal
        # result (success=False) OR the task must no longer be running.
        assert any(
            ev.payload.get("task_id") == task_id for ev in failed_events
        ), "TASK_FAILED was not published"

        terminal = orch.task_results.get(task_id)
        # With max_retries=0, the orchestrator finalizes immediately.
        assert terminal is not None and terminal.success is False
        assert task_id not in orch.running_tasks
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# 5. EventBus thread-safety smoke test
# ---------------------------------------------------------------------------


def test_event_bus_thread_safety_under_concurrent_publish():
    bus = EventBus()
    received: list[Event] = []
    received_lock = threading.Lock()

    def handler(event: Event) -> None:
        with received_lock:
            received.append(event)

    bus.subscribe(handler, EventType.SYSTEM_ALERT)

    errors: list[BaseException] = []
    stop_subscriber = threading.Event()

    def publisher_worker(worker_id: int) -> None:
        try:
            for i in range(100):
                bus.publish(
                    Event(
                        event_type=EventType.SYSTEM_ALERT,
                        source=f"worker-{worker_id}",
                        payload={"i": i},
                    )
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def churn_subscriber() -> None:
        def cb(_ev) -> None:
            return None

        try:
            while not stop_subscriber.is_set():
                bus.subscribe(cb, EventType.SYSTEM_ALERT)
                bus.unsubscribe(cb, EventType.SYSTEM_ALERT)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    publishers = [
        threading.Thread(target=publisher_worker, args=(i,)) for i in range(4)
    ]
    churn = threading.Thread(target=churn_subscriber)
    churn.start()
    for t in publishers:
        t.start()
    for t in publishers:
        t.join(timeout=10.0)
    stop_subscriber.set()
    churn.join(timeout=5.0)

    assert not errors, f"thread workers raised: {errors!r}"
    # 4 * 100 = 400 publishes
    assert len(received) == 400
    # History is bounded by EventBus._max_history = 1000
    history = bus.get_history(limit=10_000)
    assert len(history) <= 1000


# ---------------------------------------------------------------------------
# 6. Checkpoint roundtrip + corruption fallback
# ---------------------------------------------------------------------------


def test_checkpoint_roundtrip_and_corrupt_head_fallback(tmp_path):
    cp_dir = tmp_path / "checkpoints"
    mgr = CheckpointManager(checkpoint_dir=str(cp_dir))

    agents = [{"name": "a1", "status": "idle"}]
    tasks = [{"task_id": "t1", "status": "completed"}]
    pipelines = [{"pipeline_id": "p1"}]

    cp_id_first = mgr.save(
        agent_states=agents,
        task_states=tasks,
        pipeline_states=pipelines,
        event_position=42,
    )
    assert cp_id_first

    loaded = mgr.load_latest()
    assert loaded is not None
    assert isinstance(loaded, Checkpoint)
    assert loaded.checkpoint_id == cp_id_first
    assert loaded.agent_states == agents
    assert loaded.task_states == tasks
    assert loaded.pipeline_states == pipelines
    assert loaded.event_position == 42

    # Save a second good checkpoint so we can corrupt the newest one and
    # still expect load_latest to fall back to a prior good checkpoint.
    # Sleep briefly so mtimes order deterministically.
    time.sleep(0.05)
    mgr.save(
        agent_states=[{"name": "a1", "status": "busy"}],
        task_states=[{"task_id": "t2"}],
        event_position=99,
    )

    # Now write a corrupt newest checkpoint with a later mtime.
    time.sleep(0.05)
    corrupt_path = cp_dir / "cp-zzzz-corrupt.cp"
    corrupt_path.write_bytes(b"this-is-not-zlib-or-json")
    # Bump mtime so it sorts as the newest.
    now = time.time() + 10
    import os as _os

    _os.utime(corrupt_path, (now, now))

    # load_latest should skip the corrupt newest file and return the
    # previous good checkpoint instead of raising.
    fallback = mgr.load_latest()
    assert fallback is not None
    # It must be the second save (newest of the GOOD ones), event_position=99.
    assert fallback.event_position == 99


# ---------------------------------------------------------------------------
# 7. Path traversal on /api/studio/projects/{slug}/file
# ---------------------------------------------------------------------------


def test_studio_file_endpoint_rejects_path_traversal(monkeypatch, tmp_path):
    """Submitting `path=../etc/passwd` must be rejected with HTTP 400."""

    # Build a fake studio runner with a real on-disk project under tmp_path
    # so the get_project() check passes, then we attempt the traversal.
    project_root = tmp_path / "projects"
    project_root.mkdir()
    slug = "smoke-proj"
    (project_root / slug).mkdir()
    (project_root / slug / "ok.txt").write_text("hello")

    class FakeRunner:
        def __init__(self):
            self.projects_root = project_root

        def get_project(self, s: str):
            if s == slug:
                return {"slug": slug, "title": "smoke"}
            return None

    monkeypatch.setattr(web_app, "_get_studio_runner", lambda _app: FakeRunner())

    # Bypass auth (TestClient is loopback by default but be defensive).
    monkeypatch.setattr(
        web_app,
        "get_settings",
        lambda: SimpleNamespace(web_token=None, allow_unauthenticated_loopback=True),
    )

    client = TestClient(web_app.app)

    # Sanity: a normal in-tree path returns 200.
    ok = client.get(f"/api/studio/projects/{slug}/file", params={"path": "ok.txt"})
    assert ok.status_code == 200, f"control request failed: {ok.status_code} {ok.text}"

    # Traversal must be rejected with 400 (not 200, not 404, not 500).
    bad = client.get(
        f"/api/studio/projects/{slug}/file",
        params={"path": "../etc/passwd"},
    )
    assert bad.status_code == 400, f"expected 400, got {bad.status_code}: {bad.text}"
