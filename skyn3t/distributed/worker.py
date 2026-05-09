"""Distributed worker that pulls tasks from a queue."""

import asyncio
import json
import os
import signal
from typing import Any, Dict, List, Optional

from skyn3t.adapters.cli_agent import CLIAgent
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType
from skyn3t.distributed.redis_bus import RedisEventBus


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
        self._redis = None
        self._running = False
        self._task_queue = asyncio.Queue()
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
                    "started_at": str(asyncio.get_event_loop().time()),
                }
            ),
        )

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._processor_task = asyncio.create_task(self._task_processor())

        print(f"Worker {self.worker_id} started with capabilities: {self.capabilities}")

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

        print(f"Worker {self.worker_id} stopped")

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
                            "last_seen": asyncio.get_event_loop().time(),
                        }
                    ),
                )
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _task_processor(self) -> None:
        """Main loop pulling tasks from Redis queue."""
        consecutive_errors = 0
        max_backoff = 60
        while self._running:
            try:
                # Try to get task from Redis queue
                result = await self._redis.brpop("skyn3t:task_queue", timeout=5)
                if result:
                    _, task_json = result
                    task_data = json.loads(task_json)
                    await self._execute_task(task_data)
                else:
                    await asyncio.sleep(1)
                consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                print(f"Task processor error: {e}")
                backoff = min(2 ** (consecutive_errors - 1), max_backoff)
                await asyncio.sleep(backoff)

    async def _execute_task(self, task_data: Dict[str, Any]) -> None:
        """Execute a task."""
        from skyn3t.core.agent import TaskRequest

        # Capability matching: if task declares required capabilities, ensure this worker can handle them
        required = task_data.get("required_capabilities") or task_data.get("input_data", {}).get("required_capabilities")
        if required:
            if not isinstance(required, (list, tuple)):
                required = [required]
            if self.capabilities and not any(c in self.capabilities for c in required):
                print(
                    f"Worker {self.worker_id} cannot handle task {task_data.get('task_id')} "
                    f"(required={required}, have={self.capabilities}); re-enqueueing."
                )
                try:
                    await self._redis.lpush("skyn3t:task_queue", json.dumps(task_data))
                except Exception as e:
                    print(f"Failed to re-enqueue task: {e}")
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
