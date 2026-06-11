"""Async database engine and session management."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skyn3t.config.settings import get_settings

# Global engine and sessionmaker — created lazily
_engine = None
_async_session_maker = None


def _sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
    """Reduce 'database is locked' stalls under concurrent Studio + memory writes."""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def get_engine():
    """Get or create the async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict = {"echo": settings.debug}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine.sync_engine, "connect", _sqlite_pragmas)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session maker."""
    global _async_session_maker
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_maker


async def close_engine():
    """Dispose the engine. Called on shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
