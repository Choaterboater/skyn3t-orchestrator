"""Distributed worker that pulls tasks from a queue."""

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import TaskRequest, TaskResult
from skyn3t.core.events import Event, EventType
from skyn3t.distributed.redis_bus import RedisEventBus

logger = logging.getLogger("skyn3t.distributed.worker")


class Worker:
    """Distributed worker that executes tasks from a shared queue."""

    def __init__(
        self,
        worker_id: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        redis_url: Optional[str] = None,
    ):
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.capabilities = capabilities or []
        self.event_bus = RedisEventBus(redis_url)
        self._redis: Any = None
        self._running = False
        self._task_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._processor_task: Optional[asyncio.Task] = None
        self._task_count = 0

    async def start(self) -> None:
        """Start the worker."""
        await self.event_bus.initialize()

        try:
            import redis.asyncio as aioredis

            self._redis = await aioredis.from_url(
                self.event_bus._redis_url, decode_responses=True
            )
        except ImportError:
            raise ImportError("redis not installed. Run: pip install redis")

        self._running = True

        # Register worker in Redis
        await self._redis.hset(
            "skyn3t:workers",
            self.worker_id,
            json.dumps(
                {
                    "capabilities": self.capabilities,
                    "status": "idle",
                    "started_at": str(time.time()),
                }
            ),
        )

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._processor_task = asyncio.create_task(self._task_processor())

        logger.info("Worker %s started with capabilities: %s", self.worker_id, self.capabilities)

    async def stop(self) -> None:
        """Stop the worker gracefully."""
        self._running = False

        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._redis:
            await self._redis.hdel("skyn3t:workers", self.worker_id)
            await self._redis.close()

        await self.event_bus.shutdown()

        logger.info("Worker %s stopped", self.worker_id)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        while self._running:
            try:
                await self._redis.hset(
                    "skyn3t:workers",
                    self.worker_id,
                    json.dumps(
                        {
                            "capabilities": self.capabilities,
                            "status": "idle" if self._task_queue.empty() else "busy",
                            "task_count": self._task_count,
                            "last_seen": time.time(),
                        }
                    ),
                )
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Heartbeat error: %s", e)
                await asyncio.sleep(5)

    # Visibility-timeout pattern: when we pop a task we move it to a per-worker
    # processing list with BRPOPLPUSH. If we crash mid-execution, a janitor
    # (or the worker on next start) can find tasks stranded there and either
    # requeue or DLQ them. After successful execution we LREM the marker.
    PROCESSING_LIST_PREFIX = "skyn3t:processing:"
    DLQ_KEY = "skyn3t:task_queue:dlq"
    MAX_EXECUTION_ATTEMPTS = 3

    @property
    def _processing_key(self) -> str:
        return f"{self.PROCESSING_LIST_PREFIX}{self.worker_id}"

    async def _task_processor(self) -> None:
        """Main loop pulling tasks from Redis queue.

        Uses BRPOPLPUSH so the popped task lives on a worker-specific
        processing list until we LREM it on success. If we crash, the entry
        survives and can be re-queued or DLQ'd by janitors / on restart.
        """
        consecutive_errors = 0
        max_backoff = 60
        # On startup, drain anything left over from a prior crash by either
        # re-queueing (if attempt budget remains) or shipping to the DLQ.
        await self._recover_orphaned_tasks()
        while self._running:
            try:
                # BRPOPLPUSH atomically pops the queue tail and pushes onto
                # this worker's processing list. timeout=5 lets us notice
                # _running flipping false without a stuck wait.
                task_json = await self._redis.brpoplpush(
                    "skyn3t:task_queue",
                    self._processing_key,
                    timeout=5,
                )
                if task_json:
                    task_data = json.loads(task_json)
                    try:
                        await self._execute_task(task_data)
                    finally:
                        # Remove the in-flight marker regardless of outcome;
                        # _execute_task is responsible for re-queue / DLQ on
                        # failure paths.
                        try:
                            await self._redis.lrem(self._processing_key, 1, task_json)
                        except Exception:
                            pass
                else:
                    await asyncio.sleep(1)
                consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning("Task processor error: %s", e)
                backoff = min(2 ** (consecutive_errors - 1), max_backoff)
                await asyncio.sleep(backoff)

    async def _recover_orphaned_tasks(self) -> None:
        """Re-queue or DLQ tasks left on the processing list from a prior run."""
        try:
            stranded = await self._redis.lrange(self._processing_key, 0, -1)
        except Exception:
            return
        for raw in stranded or []:
            try:
                task_data = json.loads(raw)
            except Exception:
                # Garbled entry — just drop the marker.
                try:
                    await self._redis.lrem(self._processing_key, 1, raw)
                except Exception:
                    pass
                continue
            attempts = int(task_data.get("_exec_attempts", 0)) + 1
            task_data["_exec_attempts"] = attempts
            target = (
                self.DLQ_KEY if attempts > self.MAX_EXECUTION_ATTEMPTS
                else "skyn3t:task_queue"
            )
            try:
                await self._redis.lpush(target, json.dumps(task_data))
                await self._redis.lrem(self._processing_key, 1, raw)
                logger.info(
                    "Worker %s: recovered orphaned task %s → %s",
                    self.worker_id, task_data.get("task_id"), target,
                )
            except Exception as e:
                logger.warning("Failed to recover orphaned task: %s", e)

    # Maximum number of times a task can ping-pong between capability-mismatched
    # workers before it's parked on the dead-letter queue. Without this cap, a
    # task whose capability no live worker satisfies would loop forever.
    MAX_REQUEUE_ATTEMPTS = 5

    async def _execute_task(self, task_data: Dict[str, Any]) -> None:
        """Execute a task."""
        # Capability matching: if task declares required capabilities, ensure this worker can handle them
        required = task_data.get("required_capabilities") or task_data.get("input_data", {}).get("required_capabilities")
        if required:
            if not isinstance(required, (list, tuple)):
                required = [required]
            if self.capabilities and not any(c in self.capabilities for c in required):
                attempts = int(task_data.get("_requeue_attempts", 0)) + 1
                task_data["_requeue_attempts"] = attempts
                if attempts > self.MAX_REQUEUE_ATTEMPTS:
                    logger.warning(
                        "Worker %s: task %s exceeded %d re-queue attempts; "
                        "moving to DLQ (required=%s).",
                        self.worker_id, task_data.get("task_id"),
                        self.MAX_REQUEUE_ATTEMPTS, required,
                    )
                    try:
                        await self._redis.rpush("skyn3t:task_queue:dlq", json.dumps(task_data))
                    except Exception as e:
                        logger.warning("Failed to move task to DLQ: %s", e)
                    return
                logger.info(
                    "Worker %s cannot handle task %s (required=%s, have=%s); "
                    "re-enqueueing (attempt %d/%d).",
                    self.worker_id, task_data.get("task_id"), required,
                    self.capabilities, attempts, self.MAX_REQUEUE_ATTEMPTS,
                )
                try:
                    await self._redis.lpush("skyn3t:task_queue", json.dumps(task_data))
                except Exception as e:
                    logger.warning("Failed to re-enqueue task: %s", e)
                await asyncio.sleep(1)
                return

        task = TaskRequest(
            task_id=task_data["task_id"],
            title=task_data.get("title", ""),
            description=task_data.get("description", ""),
            input_data=task_data.get("input_data", {}),
            priority=task_data.get("priority", 0),
        )

        # Update worker status
        await self._redis.hset(
            "skyn3t:workers",
            self.worker_id,
            json.dumps(
                {
                    "capabilities": self.capabilities,
                    "status": "busy",
                    "current_task": task.task_id,
                    "task_count": self._task_count,
                }
            ),
        )

        # Publish task started
        self.event_bus.publish(
            Event(
                event_type=EventType.TASK_STARTED,
                source=self.worker_id,
                payload={"task_id": task.task_id, "worker": self.worker_id},
                correlation_id=task.task_id,
            )
        )

        # Execute (placeholder - in real use, the worker would have agents)
        result = TaskResult(
            task_id=task.task_id,
            success=True,
            output={"worker": self.worker_id, "executed": True},
        )

        # Publish result back
        await self._redis.publish(
            f"skyn3t:results:{task.task_id}",
            json.dumps(
                {
                    "task_id": task.task_id,
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                    "worker": self.worker_id,
                }
            ),
        )

        self._task_count += 1

        # Update worker status back to idle
        await self._redis.hset(
            "skyn3t:workers",
            self.worker_id,
            json.dumps(
                {
                    "capabilities": self.capabilities,
                    "status": "idle",
                    "task_count": self._task_count,
                }
            ),
        )


async def main() -> None:
    """Run a worker process."""
    import argparse

    parser = argparse.ArgumentParser(description="SkyN3t Distributed Worker")
    parser.add_argument("--redis-url", default=None, help="Redis URL")
    parser.add_argument("--capabilities", nargs="+", default=[], help="Agent capabilities")
    parser.add_argument("--worker-id", default=None, help="Worker ID")
    args = parser.parse_args()

    worker = Worker(
        worker_id=args.worker_id,
        capabilities=args.capabilities,
        redis_url=args.redis_url,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.start()

    # Keep running
    while worker._running:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
