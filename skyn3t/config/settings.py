"""Application configuration and settings."""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_SECRET_KEY_PLACEHOLDER = "change-me-in-production"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "SkyN3t Orchestrator"
    app_version: str = "0.1.0"
    debug: bool = Field(default=False, alias="DEBUG")
    secret_key: str = Field(default="change-me-in-production", alias="SECRET_KEY")

    # Paths
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    logs_dir: Path = Field(default=Path("./logs"), alias="LOGS_DIR")
    projects_dir: Path = Field(default=Path("./projects"), alias="PROJECTS_DIR")

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/skyn3t.db", alias="DATABASE_URL"
    )
    vector_db_path: str = Field(default="./data/vector_db", alias="VECTOR_DB_PATH")

    # Redis / Message Bus
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    use_redis: bool = Field(default=False, alias="USE_REDIS")

    # API Keys
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")
    kimi_api_key: Optional[str] = Field(default=None, alias="KIMI_API_KEY")

    # LLM Defaults
    default_llm_provider: str = Field(default="openai", alias="DEFAULT_LLM_PROVIDER")
    default_model: str = Field(default="gpt-4-turbo-preview", alias="DEFAULT_MODEL")
    llm_backend: str = Field(default="auto", alias="SKYN3T_LLM_BACKEND")
    llm_model: Optional[str] = Field(default=None, alias="SKYN3T_LLM_MODEL")

    # Orchestrator
    max_concurrent_tasks: int = Field(default=10, alias="MAX_CONCURRENT_TASKS")
    task_timeout_seconds: int = Field(default=300, alias="TASK_TIMEOUT_SECONDS")
    heartbeat_interval_seconds: int = Field(default=30, alias="HEARTBEAT_INTERVAL_SECONDS")
    self_heal_enabled: bool = Field(default=True, alias="SELF_HEAL_ENABLED")
    # Per-agent queue depth cap. submit_task drops with QUEUE_BACKPRESSURE_REJECT
    # rather than buffering more requests when this is exceeded. 0 = unbounded.
    max_queue_depth: int = Field(default=1000, alias="MAX_QUEUE_DEPTH")

    # RAG
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2", alias="EMBEDDING_MODEL"
    )
    chunk_size: int = Field(default=1000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=200, alias="CHUNK_OVERLAP")
    top_k_retrieval: int = Field(default=5, alias="TOP_K_RETRIEVAL")

    # Discord (all optional — control surface; bot/webhook still gated by token)
    discord_application_id: Optional[str] = Field(default=None, alias="SKYN3T_DISCORD_APP_ID")
    discord_public_key: Optional[str] = Field(default=None, alias="SKYN3T_DISCORD_PUBLIC_KEY")
    discord_bot_channel_id: Optional[str] = Field(default=None, alias="SKYN3T_DISCORD_CHANNEL_ID")
    discord_token: Optional[str] = Field(default=None, alias="DISCORD_TOKEN")
    discord_admin_secret: Optional[str] = Field(default=None, alias="SKYN3T_DISCORD_ADMIN_SECRET")

    # Telegram studio control surface (long-polling, no public URL needed)
    telegram_token: Optional[str] = Field(default=None, alias="SKYN3T_TELEGRAM_TOKEN")
    telegram_user_id: Optional[str] = Field(default=None, alias="SKYN3T_TELEGRAM_USER_ID")

    # OpenRouter — single API key for dozens of models. Used as a
    # fallback when CLI subprocesses fail (App.jsx-stub failure mode)
    # and as a fast-path for known-problematic entrypoint files.
    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    max_build_cost_usd: float = Field(default=1.0, alias="SKYN3T_MAX_BUILD_COST_USD")
    model_routing_path: Path = Field(
        default=Path("./data/model_routing.json"),
        alias="SKYN3T_MODEL_ROUTING_PATH",
    )

    # Legacy master switch for automatic Cortex handling. When false,
    # system proposals stay fully review-gated and selective auto-triage
    # rules do not run.
    cortex_auto_approve_system: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_APPROVE_SYSTEM"
    )
    cortex_auto_reject_duplicates: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_REJECT_DUPLICATES"
    )
    cortex_auto_reject_low_signal_ingest: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_REJECT_LOW_SIGNAL_INGEST"
    )
    cortex_auto_approve_safe_ingest: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_APPROVE_SAFE_INGEST"
    )
    cortex_auto_triage_duplicate_window_seconds: int = Field(
        default=86_400, alias="SKYN3T_CORTEX_AUTO_TRIAGE_DUPLICATE_WINDOW_SECONDS"
    )
    cortex_auto_triage_min_ingest_topic_length: int = Field(
        default=6, alias="SKYN3T_CORTEX_AUTO_TRIAGE_MIN_INGEST_TOPIC_LENGTH"
    )
    cortex_auto_triage_max_safe_ingest_limit: int = Field(
        default=3, alias="SKYN3T_CORTEX_AUTO_TRIAGE_MAX_SAFE_INGEST_LIMIT"
    )
    # GitHub repo-scout ingest is operational knowledge gathering — auto-run
    # without a manual ingest approval step. Follow-on SkyN3t code changes still
    # require a separate feature proposal approval.
    cortex_auto_approve_scout_ingest: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_APPROVE_SCOUT_INGEST"
    )
    cortex_auto_triage_max_scout_ingest_limit: int = Field(
        default=10, alias="SKYN3T_CORTEX_AUTO_TRIAGE_MAX_SCOUT_INGEST_LIMIT"
    )
    cortex_scout_spawn_features: bool = Field(
        default=True, alias="SKYN3T_CORTEX_SCOUT_SPAWN_FEATURES"
    )
    cortex_scout_spawn_min_ingested: int = Field(
        default=1, alias="SKYN3T_CORTEX_SCOUT_SPAWN_MIN_INGESTED"
    )

    # Execution backend for code agent: inline (fast, no isolation),
    # docker (real sandbox), or auto (probe docker, fall back to inline).
    execution_backend: str = Field(default="auto", alias="SKYN3T_EXECUTION_BACKEND")

    # Public URL — used in notification embeds (Discord etc.) so users can
    # click through to the dashboard. Leave unset to fall back to
    # http://<web_host>:<web_port>, which is fine for local-only use but
    # won't resolve from a phone. Set this once you have a Cloudflare
    # Tunnel or stable public hostname.
    public_url: Optional[str] = Field(default=None, alias="SKYN3T_PUBLIC_URL")

    # Web
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=6660, alias="WEB_PORT")
    cors_origins: List[str] = Field(default=["*"], alias="CORS_ORIGINS")
    web_token: Optional[str] = Field(default=None, alias="SKYN3T_WEB_TOKEN")

    # Security
    security_enabled: bool = Field(default=True, alias="SECURITY_ENABLED")
    master_key: Optional[str] = Field(default=None, alias="SKYN3T_MASTER_KEY")
    allow_ephemeral_master_key: bool = Field(
        default=False,
        alias="SKYN3T_ALLOW_EPHEMERAL_MASTER_KEY",
    )
    secret_storage_path: Path = Field(default=Path("./data/secrets.json"), alias="SECRET_STORAGE_PATH")
    audit_log_dir: Path = Field(default=Path("./logs/audit"), alias="AUDIT_LOG_DIR")
    audit_max_entries_per_file: int = Field(default=10000, alias="AUDIT_MAX_ENTRIES_PER_FILE")
    default_agent_role: str = Field(default="developer", alias="DEFAULT_AGENT_ROLE")
    policy_file: Optional[Path] = Field(default=None, alias="POLICY_FILE")
    sandbox_default_cpu_time: float = Field(default=60.0, alias="SANDBOX_DEFAULT_CPU_TIME")
    sandbox_default_memory_mb: int = Field(default=512, alias="SANDBOX_DEFAULT_MEMORY_MB")
    sandbox_default_file_size_mb: int = Field(default=128, alias="SANDBOX_DEFAULT_FILE_SIZE_MB")
    sandbox_default_timeout: float = Field(default=300.0, alias="SANDBOX_DEFAULT_TIMEOUT")
    sandbox_capture_syscalls: bool = Field(default=False, alias="SANDBOX_CAPTURE_SYSCALLS")
    sandbox_cleanup_temp: bool = Field(default=True, alias="SANDBOX_CLEANUP_TEMP")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v]
        if v is None:
            return []
        return [str(v).strip()]

    @field_validator(
        "data_dir",
        "logs_dir",
        "projects_dir",
        "secret_storage_path",
        "audit_log_dir",
        "policy_file",
        mode="before",
    )
    @classmethod
    def parse_paths(cls, v: Any) -> Any:
        if v is None:
            return None
        return Path(v).expanduser().absolute()

    def ensure_directories(self) -> None:
        """Create necessary data directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_dir.mkdir(parents=True, exist_ok=True)
        Path(self.vector_db_path).mkdir(parents=True, exist_ok=True)


def resolve_api_base(settings: Settings | None = None) -> str:
    """Return the SkyN3t API base URL for CLI/REPL clients."""
    if url := os.environ.get("SKYN3T_API_URL"):
        return url.rstrip("/")
    cfg = settings or get_settings()
    host = "localhost" if cfg.web_host in ("0.0.0.0", "::") else cfg.web_host
    return f"http://{host}:{cfg.web_port}"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    # Backwards compatibility: accept old SkyN3t_MASTER_KEY env var as a
    # fallback if SKYN3T_MASTER_KEY is not set. Remove after one release.
    if "SKYN3T_MASTER_KEY" not in os.environ and "SkyN3t_MASTER_KEY" in os.environ:
        os.environ["SKYN3T_MASTER_KEY"] = os.environ["SkyN3t_MASTER_KEY"]
    settings = Settings()
    settings.ensure_directories()
    if not settings.secret_key or settings.secret_key == _SECRET_KEY_PLACEHOLDER:
        logger.warning(
            "SECRET_KEY is empty or set to the default placeholder; "
            "set a strong SECRET_KEY before running in production."
        )
    return settings
