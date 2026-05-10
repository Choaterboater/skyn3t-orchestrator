"""Redis-backed distributed event bus."""

import asyncio
import json
import logging
from typing import Any, Optional

from skyn3t.config.settings import get_settings
from skyn3t.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class RedisEventBus(EventBus):
    """Event bus backed by Redis pub/sub for distributed operation."""

    def __init__(self, redis_url: Optional[str] = None):
        super().__init__()
        self._redis_url = redis_url or get_settings().redis_url
        self._redis: Any = None
        self._pubsub: Any = None
        self._listener_task: Optional[asyncio.Task] = None
        self._running = False
        self._local_only = False
        # Strong refs to in-flight publish tasks. Without this, asyncio.create_task
        # returns a coroutine the GC may collect before it runs, silently dropping
        # the published event. set.discard via done callback releases on completion.
        self._publish_tasks: "set[asyncio.Task]" = set()

    async def initialize(self) -> None:
        """Initialize Redis connection."""
        try:
            import redis.asyncio as aioredis

            self._redis = await aioredis.from_url(
                self._redis_url, decode_responses=True
            )
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe("skyn3t:events")
            self._running = True
            self._listener_task = asyncio.create_task(self._redis_listener())
        except ImportError:
            logger.warning("redis package not installed; RedisEventBus running in local-only mode.")
            self._local_only = True
        except Exception as e:
            logger.warning("Redis connection failed: %s. Running in local-only mode.", e)
            self._local_only = True

    async def _redis_listener(self) -> None:
        """Listen for events from Redis and route locally."""
        while self._running:
            try:
                message = await self._pubsub.get_message(timeout=1.0)
                if message and message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        event = Event(
                            event_type=EventType[data["event_type"]],
                            source=data["source"],
                            payload=data["payload"],
                            target=data.get("target"),
                            correlation_id=data.get("correlation_id"),
                            priority=data.get("priority", 0),
                        )
                        # Don't re-publish to Redis (prevent loops)
                        super().publish(event)
                    except Exception:
                        logger.exception("Subscriber failed to handle Redis message; continuing.")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Redis listener error")
                await asyncio.sleep(1)

    def publish(self, event: Event) -> None:
        """Publish event locally AND to Redis."""
        super().publish(event)

        if not self._local_only and self._redis:
            try:
                task = asyncio.create_task(self._publish_to_redis(event))
            except RuntimeError:
                # Called from a thread/context with no running loop; the local
                # publish above already happened, so just skip the Redis fan-out.
                return
            self._publish_tasks.add(task)
            task.add_done_callback(self._publish_tasks.discard)

    async def _publish_to_redis(self, event: Event) -> None:
        """Publish event to Redis channel."""
        try:
            data = json.dumps(event.to_dict(), default=str)
            await self._redis.publish("skyn3t:events", data)
        except Exception as e:
            logger.warning("Redis publish failed: %s", e)

    async def shutdown(self) -> None:
        """Shutdown Redis connection."""
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()

    def is_distributed(self) -> bool:
        return not self._local_only
