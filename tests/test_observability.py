"""Tests for the SkyN3t observability layer."""

import asyncio
import time

import pytest

from skyn3t.observability.health import (
    HealthCheck,
    HealthRegistry,
    HealthStatus,
    get_health_registry,
    reset_health_registry,
)
from skyn3t.observability.logging import (
    bind_task_context,
    clear_context,
    get_logger,
    log_agent_event,
    log_cli_execution,
    log_task_event,
)
from skyn3t.observability.metrics import (
    generate_metrics,
    get_collector,
    reset_collector,
    timed,
)
from skyn3t.observability.tracing import (
    ConsoleExporter,
    SpanStatus,
    TraceContext,
    Tracer,
    TraceSpan,
    trace_task,
)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_collector_singleton(self):
        reset_collector()
        c1 = get_collector()
        c2 = get_collector()
        assert c1 is c2

    def test_counter_methods(self):
        reset_collector()
        col = get_collector()
        # Should not raise even with no-op stubs
        col.record_task_submitted("agent_a", "test")
        col.record_task_completed("agent_a", "test", 0.5)
        col.record_task_failed("agent_a", "test", reason="boom")

    def test_gauge_methods(self):
        reset_collector()
        col = get_collector()
        col.set_active_agents(5)
        col.set_active_tasks("agent_a", 2)
        col.set_queue_depth("agent_a", "test", 3)

    def test_histogram_methods(self):
        reset_collector()
        col = get_collector()
        col.record_cli_execution("git", 1.2)

    def test_generate_metrics(self):
        reset_collector()
        payload = generate_metrics()
        assert isinstance(payload, bytes)
        assert b"skyn3t" in payload or b"not installed" in payload

    def test_timed_decorator_sync(self):
        @timed("cli_execution", labels={"tool_name": "git"})
        def slow():
            time.sleep(0.01)
            return 42

        result = slow()
        assert result == 42

    @pytest.mark.asyncio
    async def test_timed_decorator_async(self):
        @timed("cli_execution", labels={"tool_name": "git"})
        async def slow():
            await asyncio.sleep(0.01)
            return 42

        result = await slow()
        assert result == 42


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
class TestTracing:
    def test_span_lifecycle(self):
        span = TraceSpan(name="test")
        assert span.status == SpanStatus.UNKNOWN
        assert span.duration_ms is None
        span.finish(status=SpanStatus.OK)
        assert span.status == SpanStatus.OK
        assert span.duration_ms is not None
        assert span.duration_ms >= 0

    def test_span_dict(self):
        span = TraceSpan(name="test", attributes={"foo": "bar"})
        span.finish(SpanStatus.OK)
        d = span.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "ok"
        assert d["attributes"]["foo"] == "bar"

    def test_trace_context(self):
        ctx = TraceContext(trace_id="abc", parent_span_id="parent1")
        assert ctx.trace_id == "abc"
        assert ctx.parent_span_id == "parent1"
        injected = ctx.inject()
        assert injected["trace_id"] == "abc"

        extracted = TraceContext.extract(injected)
        assert extracted.trace_id == "abc"

    def test_tracer_span(self):
        tracer = Tracer()
        span = tracer.start_span("op1")
        assert span.name == "op1"
        child = tracer.start_span("op2")
        assert child.parent_id == span.id
        tracer.end_span(child)
        tracer.end_span(span)
        assert span in tracer._finished

    def test_tracer_recent_spans(self):
        tracer = Tracer()
        span = tracer.start_span("recent")
        tracer.end_span(span)
        recent = tracer.get_recent_spans(limit=10)
        assert len(recent) == 1
        assert recent[0].name == "recent"

    @pytest.mark.asyncio
    async def test_tracer_async_context(self):
        tracer = Tracer()
        async with tracer.span("async_op", attributes={"key": "val"}) as span:
            assert span.name == "async_op"
            span.add_event("halfway")
        assert span.status == SpanStatus.OK
        assert len(span.events) == 1

    @pytest.mark.asyncio
    async def test_tracer_async_error(self):
        tracer = Tracer()
        with pytest.raises(ValueError):
            async with tracer.span("fail") as span:
                raise ValueError("boom")
        assert span.status == SpanStatus.ERROR

    def test_trace_task_decorator(self):
        @trace_task("my_task", attributes={"kind": "demo"})
        def do_work():
            return 123

        assert do_work() == 123

    @pytest.mark.asyncio
    async def test_trace_task_decorator_async(self):
        @trace_task("my_async_task")
        async def do_work():
            return 456

        assert await do_work() == 456

    def test_console_exporter(self, caplog):
        tracer = Tracer()
        span = tracer.start_span("exported")
        tracer.end_span(span)
        exporter = ConsoleExporter(tracer)
        with caplog.at_level("INFO", logger="skyn3t.observability.tracing"):
            exporter.export(span)
        assert "exported" in caplog.text


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class TestLogging:
    def test_get_logger(self):
        log = get_logger("test.logger")
        assert log is not None

    def test_log_agent_event(self):
        log_agent_event("registered", "claude", "cli", details={"model": "opus"})

    def test_log_task_event(self):
        log_task_event(
            "completed",
            task_id="t1",
            agent_name="claude",
            duration_ms=150.5,
            success=True,
        )

    def test_log_cli_execution(self):
        log_cli_execution(
            "git",
            "git log --oneline",
            duration_ms=50.0,
            success=True,
            exit_code=0,
        )

    def test_context_helpers(self):
        bind_task_context(task_id="abc", agent_name="kimi")
        clear_context()
        # Should not raise


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_check_dataclass(self):
        hc = HealthCheck(name="disk", status=HealthStatus.HEALTHY, details={"free": 100})
        d = hc.to_dict()
        assert d["name"] == "disk"
        assert d["status"] == "healthy"

    def test_registry_register_and_run(self):
        registry = HealthRegistry()
        async def always_ok():
            return HealthCheck(name="ok", status=HealthStatus.HEALTHY)

        registry.register("ok", always_ok)
        result = asyncio.run(registry.run_check("ok"))
        assert result is not None
        assert result.status == HealthStatus.HEALTHY

    def test_registry_run_all(self):
        registry = HealthRegistry()
        async def ok():
            return HealthCheck(name="ok", status=HealthStatus.HEALTHY)

        registry.register("a", ok)
        registry.register("b", ok)
        agg = asyncio.run(registry.run_all())
        assert agg["status"] == "healthy"
        assert agg["summary"]["total"] == 2
        assert agg["summary"]["healthy"] == 2

    def test_registry_degraded(self):
        registry = HealthRegistry()
        async def ok():
            return HealthCheck(name="ok", status=HealthStatus.HEALTHY)

        async def bad():
            return HealthCheck(name="bad", status=HealthStatus.UNHEALTHY)

        registry.register("ok", ok)
        registry.register("bad", bad)
        agg = asyncio.run(registry.run_all())
        assert agg["status"] == "unhealthy"

    def test_registry_built_in_checks(self):
        reset_health_registry()
        registry = get_health_registry()
        registry.register_built_in_checks()
        assert "disk" in registry._checks
        assert "memory" in registry._checks
        assert "cpu" in registry._checks
        assert "cli_tools" in registry._checks

    def test_registry_get_last_results(self):
        registry = HealthRegistry()
        async def ok():
            return HealthCheck(name="ok", status=HealthStatus.HEALTHY)

        registry.register("ok", ok)
        asyncio.run(registry.run_all())
        last = registry.get_last_results()
        assert "ok" in last
