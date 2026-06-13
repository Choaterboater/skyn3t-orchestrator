"""Application configuration and settings."""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from skyn3t.config.env_file import warn_env_file_permissions

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

    # Slack bot (Socket Mode when SLACK_APP_TOKEN is also set)
    slack_bot_token: Optional[str] = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_app_token: Optional[str] = Field(default=None, alias="SLACK_APP_TOKEN")

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
    cortex_auto_approve_safe_tuning: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_APPROVE_SAFE_TUNING"
    )
    cortex_auto_approve_build_pattern_bias: bool = Field(
        default=True, alias="SKYN3T_CORTEX_AUTO_APPROVE_BUILD_PATTERN_BIAS"
    )
    cortex_auto_triage_max_scout_ingest_limit: int = Field(
        default=10, alias="SKYN3T_CORTEX_AUTO_TRIAGE_MAX_SCOUT_INGEST_LIMIT"
    )
    # Max self-edit (feature/code_patch) applies to spawn per retriage sweep.
    # Each apply runs the full test suite, so a deep backlog is drained in
    # bounded batches instead of all at once. 0 = unlimited.
    cortex_self_edit_retriage_batch: int = Field(
        default=5, alias="SKYN3T_CORTEX_SELF_EDIT_RETRIAGE_BATCH"
    )
    cortex_scout_spawn_features: bool = Field(
        default=True, alias="SKYN3T_CORTEX_SCOUT_SPAWN_FEATURES"
    )
    cortex_scout_spawn_min_ingested: int = Field(
        default=1, alias="SKYN3T_CORTEX_SCOUT_SPAWN_MIN_INGESTED"
    )
    cortex_scout_skip_when_busy: bool = Field(
        default=True, alias="SKYN3T_CORTEX_SCOUT_SKIP_WHEN_BUSY"
    )
    cortex_scout_run_timeout_seconds: int = Field(
        default=300, alias="SKYN3T_CORTEX_SCOUT_RUN_TIMEOUT_SECONDS"
    )
    cortex_scout_fit_queries: List[str] = Field(
        default_factory=lambda: [
            "multi agent orchestrator cli memory rag",
            "cortex autonomy self-healing proposal review agent learning",
            "design system ui components app builder",
            "game framework rendering ui workflow",
            "developer workflow automation testing packaging",
        ],
        alias="SKYN3T_CORTEX_SCOUT_FIT_QUERIES",
    )
    cortex_scout_default_limit: int = Field(
        default=2, alias="SKYN3T_CORTEX_SCOUT_DEFAULT_LIMIT"
    )
    cortex_scout_include_competitive_queries: bool = Field(
        default=True,
        alias="SKYN3T_CORTEX_SCOUT_COMPETITIVE",
    )

    # Autonomous loops — scout schedule + optional Studio builds without CLI
    autonomous_learning: bool = Field(default=True, alias="SKYN3T_AUTONOMOUS_LEARNING")
    autonomous_builds: bool = Field(default=False, alias="SKYN3T_AUTONOMOUS_BUILDS")
    # Appended as "Domain focus: …" to every autonomous build brief.
    autonomous_brief_domain: str = Field(
        default="", alias="SKYN3T_AUTONOMOUS_BRIEF_DOMAIN"
    )
    # 0 = no count limit; the daily budget (USD) governs instead.
    autonomous_build_daily_cap: int = Field(default=3, alias="SKYN3T_AUTONOMOUS_BUILD_DAILY_CAP")
    # When true (and full-auto/no-approval is on), repo self-edit proposals
    # (feature / code_patch, incl. user dashboard ideas) auto-approve and apply.
    # Applies happen on a throwaway `skyn3t/auto/<id>` branch behind a pytest
    # gate — never directly on main — so this stays safe to leave on.
    auto_apply_self_edits: bool = Field(default=False, alias="SKYN3T_AUTO_APPLY_SELF_EDITS")
    autonomous_build_interval_seconds: int = Field(
        default=900, alias="SKYN3T_AUTONOMOUS_BUILD_INTERVAL_SECONDS"
    )
    autonomous_build_daily_budget_usd: float = Field(
        default=5.0, alias="SKYN3T_AUTONOMOUS_BUILD_DAILY_BUDGET_USD"
    )
    autonomous_scout_schedule: str = Field(
        default="interval:12h", alias="SKYN3T_AUTONOMOUS_SCOUT_SCHEDULE"
    )
    autonomous_proof_run: bool = Field(
        default=True, alias="SKYN3T_AUTONOMOUS_PROOF_RUN"
    )
    autonomous_min_reviewer_score: int = Field(
        default=85, alias="SKYN3T_AUTONOMOUS_MIN_REVIEWER_SCORE"
    )
    autonomous_quality_retry: bool = Field(
        default=True, alias="SKYN3T_AUTONOMOUS_QUALITY_RETRY"
    )
    autonomous_build_max_retries: int = Field(
        default=3, alias="SKYN3T_AUTONOMOUS_BUILD_MAX_RETRIES"
    )
    autonomous_queue_max_depth: int = Field(
        default=100, alias="SKYN3T_AUTONOMOUS_QUEUE_MAX_DEPTH"
    )
    autonomous_resume_interrupted: bool = Field(
        default=True, alias="SKYN3T_AUTONOMOUS_RESUME_INTERRUPTED"
    )
    # Parallel agent fleet — concurrent autonomous learn + build workers
    agent_fleet_size: int = Field(default=0, alias="SKYN3T_AGENT_FLEET_SIZE")
    agent_fleet_learning: int = Field(default=1, alias="SKYN3T_AGENT_FLEET_LEARNING")
    agent_fleet_tick_seconds: int = Field(
        default=30, alias="SKYN3T_AGENT_FLEET_TICK_SECONDS"
    )
    agent_fleet_max_concurrent_builds: int = Field(
        default=5, alias="SKYN3T_AGENT_FLEET_MAX_CONCURRENT_BUILDS"
    )
    cortex_scout_defer_boot_seconds: int = Field(
        default=120, alias="SKYN3T_CORTEX_SCOUT_DEFER_BOOT_SECONDS"
    )
    # Never-stop improvement flywheel (default on)
    continuous_improvement: bool = Field(
        default=True, alias="SKYN3T_CONTINUOUS_IMPROVEMENT"
    )
    improvement_tick_seconds: int = Field(
        default=600, alias="SKYN3T_IMPROVEMENT_TICK_SECONDS"
    )
    improvement_score_regression_threshold: float = Field(
        default=70.0, alias="SKYN3T_IMPROVEMENT_SCORE_REGRESSION"
    )
    improvement_stack_score_window: int = Field(
        default=8, alias="SKYN3T_IMPROVEMENT_SCORE_WINDOW"
    )
    improvement_competitive_practice_daily_cap: int = Field(
        default=1, alias="SKYN3T_IMPROVEMENT_COMPETITIVE_PRACTICE_DAILY_CAP"
    )
    improvement_proof_retry_daily_cap: int = Field(
        default=2, alias="SKYN3T_IMPROVEMENT_PROOF_RETRY_DAILY_CAP"
    )
    # Never-stop watchdog — auto-restart dead autonomy tasks (default on with improvement)
    never_stop: bool = Field(default=True, alias="SKYN3T_NEVER_STOP")
    never_stop_queue_empty_seconds: int = Field(
        default=300, alias="SKYN3T_NEVER_STOP_QUEUE_EMPTY_SECONDS"
    )
    skills_hub_auto_install: bool = Field(
        default=True, alias="SKYN3T_SKILLS_HUB_AUTO_INSTALL"
    )
    # Skip Studio architect/designer gates and auto-triage Cortex ingest/tuning.
    # Also enabled implicitly when SKYN3T_AUTONOMOUS_BUILDS=1 (unless
    # SKYN3T_AUTO_APPROVE=0). Synonyms: SKYN3T_NO_APPROVAL=1,
    # SKYN3T_AUTO_APPROVE_STUDIO=1.
    auto_approve: bool = Field(default=False, alias="SKYN3T_AUTO_APPROVE")

    # Consciousness snapshots — durable orchestrator state
    snapshot_enabled: bool = Field(default=True, alias="SKYN3T_SNAPSHOT_ENABLED")
    snapshot_interval_seconds: int = Field(
        default=300, alias="SKYN3T_SNAPSHOT_INTERVAL_SECONDS"
    )
    snapshot_max_kept: int = Field(default=10, alias="SKYN3T_SNAPSHOT_MAX_KEPT")
    restore_on_boot: bool = Field(default=True, alias="SKYN3T_RESTORE_ON_BOOT")
    snapshot_dir: Path = Field(default=Path("./data/checkpoints"), alias="SKYN3T_SNAPSHOT_DIR")

    # Studio per-project token budget (estimated chars/4). 0 = disabled.
    # Inspired by Forge cost caps — stops runaway LLM spend mid-pipeline.
    studio_token_budget: int = Field(default=0, alias="SKYN3T_STUDIO_TOKEN_BUDGET")

    # CodeAgent Python execution: auto (Docker pool when available, else inline),
    # inline, docker, or docker-pool.
    execution_backend: str = Field(default="auto", alias="SKYN3T_EXECUTION_BACKEND")

    # Docker sandbox hardening (defense-in-depth for code execution)
    docker_hardening: bool = Field(default=True, alias="SKYN3T_DOCKER_HARDENING")
    docker_user: str = Field(default="65534:65534", alias="SKYN3T_DOCKER_USER")
    docker_cpus: float = Field(default=1.0, alias="SKYN3T_DOCKER_CPUS")
    docker_pids_limit: int = Field(default=64, alias="SKYN3T_DOCKER_PIDS_LIMIT")
    docker_no_new_privs: bool = Field(default=True, alias="SKYN3T_DOCKER_NO_NEW_PRIVS")
    docker_cap_drop_all: bool = Field(default=True, alias="SKYN3T_DOCKER_CAP_DROP_ALL")
    docker_pool_recycle_after: int = Field(default=50, alias="SKYN3T_DOCKER_POOL_RECYCLE_AFTER")

    # Retention / pruning (H24). 0 disables pruning for that entity.
    retention_logs_days: int = Field(default=7, alias="SKYN3T_RETENTION_LOGS_DAYS")
    retention_messages_days: int = Field(default=30, alias="SKYN3T_RETENTION_MESSAGES_DAYS")
    retention_experience_days: int = Field(default=90, alias="SKYN3T_RETENTION_EXPERIENCE_DAYS")
    retention_completed_tasks_days: int = Field(default=30, alias="SKYN3T_RETENTION_COMPLETED_TASKS_DAYS")

    # Public URL — used in notification embeds (Discord etc.) so users can
    # click through to the dashboard. Leave unset to fall back to
    # http://<web_host>:<web_port>, which is fine for local-only use but
    # won't resolve from a phone. Set this once you have a Cloudflare
    # Tunnel or stable public hostname.
    public_url: Optional[str] = Field(default=None, alias="SKYN3T_PUBLIC_URL")

    # Web
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=6660, alias="WEB_PORT")
    cors_origins: List[str] = Field(
        default=["http://localhost:5173", "http://localhost:5180"],
        alias="CORS_ORIGINS",
    )
    web_token: Optional[str] = Field(default=None, alias="SKYN3T_WEB_TOKEN")
    # SECURITY: by default the web control plane requires SKYN3T_WEB_TOKEN.
    # Setting this to true restores the legacy loopback-only behavior. It is
    # convenient for local development but dangerous if the host is shared or
    # the dashboard is ever exposed beyond localhost.
    allow_unauthenticated_loopback: bool = Field(
        default=False, alias="SKYN3T_ALLOW_UNAUTHENTICATED_LOOPBACK"
    )
    # SECURITY: /api/exec runs arbitrary code through the configured sandbox.
    # Disabled by default; enable only on trusted dev hosts.
    allow_exec_api: bool = Field(default=False, alias="SKYN3T_ALLOW_EXEC_API")

    # Security
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

    @field_validator("cortex_scout_fit_queries", mode="before")
    @classmethod
    def parse_scout_fit_queries(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        if isinstance(v, list):
            return [str(part).strip() for part in v if str(part).strip()]
        if v is None:
            return []
        text = str(v).strip()
        return [text] if text else []

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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_falsy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def auto_approve_enabled(settings: Settings | None = None) -> bool:
    """True when human approval gates should be bypassed for Studio + Cortex.

    Explicit opt-in: ``SKYN3T_AUTO_APPROVE=1``, ``SKYN3T_NO_APPROVAL=1``, or
    ``SKYN3T_AUTO_APPROVE_STUDIO=1``. Implicit opt-in:
    ``SKYN3T_AUTONOMOUS_BUILDS=1`` unless ``SKYN3T_AUTO_APPROVE=0``.
    Repo self-edits (``code_patch`` / ``feature`` ideas) are never
    auto-approved here — they stay review-gated.
    """
    cfg = settings or get_settings()
    if (
        getattr(cfg, "auto_approve", False)
        or _env_truthy("SKYN3T_NO_APPROVAL")
        or _env_truthy("SKYN3T_AUTO_APPROVE_STUDIO")
    ):
        return True
    if _env_falsy("SKYN3T_AUTO_APPROVE"):
        return False
    return bool(getattr(cfg, "autonomous_builds", False))


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
    warn_env_file_permissions()
    return settings
