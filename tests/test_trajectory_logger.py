"""Tests for the trajectory logger."""

import json
import tempfile
from pathlib import Path

from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.observability.trajectory_logger import TrajectoryLogger


class TestTrajectoryLogger:
    def test_subscribes_to_event_bus(self):
        bus = EventBus()
        logger = TrajectoryLogger()
        logger.subscribe(bus)
        # Should not raise

    def test_captures_task_lifecycle(self):
        bus = EventBus()
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = TrajectoryLogger(output_dir=Path(tmpdir))
            logger.subscribe(bus)

            bus.publish(Event(
                event_type=EventType.TASK_STARTED,
                source="code_agent",
                payload={"task_id": "t-1", "session_id": "s-1", "stage": "code"},
            ))
            bus.publish(Event(
                event_type=EventType.LLM_EXCHANGE,
                source="code_agent",
                payload={"task_id": "t-1", "backend": "openai", "total_tokens": 500},
            ))
            bus.publish(Event(
                event_type=EventType.TASK_COMPLETED,
                source="code_agent",
                payload={"task_id": "t-1"},
            ))

            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) == 1
            lines = files[0].read_text().strip().splitlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["task_id"] == "t-1"
            assert record["agent"] == "code_agent"
            assert record["outcome"] == "success"
            assert len(record["events"]) == 1
            assert record["events"][0]["type"] == "llm_call"

    def test_export_with_filters(self):
        bus = EventBus()
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = TrajectoryLogger(output_dir=Path(tmpdir))
            logger.subscribe(bus)

            bus.publish(Event(
                event_type=EventType.TASK_STARTED,
                source="designer",
                payload={"task_id": "t-2"},
            ))
            bus.publish(Event(
                event_type=EventType.TASK_FAILED,
                source="designer",
                payload={"task_id": "t-2", "error": "timeout"},
            ))

            out = Path(tmpdir) / "export.jsonl"
            count = logger.export_jsonl(out, agent="designer")
            assert count == 1
            record = json.loads(out.read_text().strip())
            assert record["agent"] == "designer"
            assert record["outcome"] == "failure"

    def test_list_files(self):
        bus = EventBus()
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = TrajectoryLogger(output_dir=Path(tmpdir))
            logger.subscribe(bus)
            bus.publish(Event(
                event_type=EventType.TASK_STARTED,
                source="agent",
                payload={"task_id": "t-3"},
            ))
            bus.publish(Event(
                event_type=EventType.TASK_COMPLETED,
                source="agent",
                payload={"task_id": "t-3"},
            ))
            files = logger.list_files()
            assert len(files) == 1
            assert files[0]["records"] == 1
