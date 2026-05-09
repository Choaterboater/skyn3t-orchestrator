"""Application configuration and settings."""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    # Orchestrator
    max_concurrent_tasks: int = Field(default=10, alias="MAX_CONCURRENT_TASKS")
    task_timeout_seconds: int = Field(default=300, alias="TASK_TIMEOUT_SECONDS")
    heartbeat_interval_seconds: int = Field(default=30, alias="HEARTBEAT_INTERVAL_SECONDS")
    self_heal_enabled: bool = Field(default=True, alias="SELF_HEAL_ENABLED")

    # RAG
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2", alias="EMBEDDING_MODEL"
    )
    chunk_size: int = Field(default=1000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=200, alias="CHUNK_OVERLAP")
    top_k_retrieval: int = Field(default=5, alias="TOP_K_RETRIEVAL")

    # Web
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=6660, alias="WEB_PORT")
    cors_origins: List[str] = Field(default=["*"], alias="CORS_ORIGINS")

    # Security
    security_enabled: bool = Field(default=True, alias="SECURITY_ENABLED")
    master_key: Optional[str] = Field(default=None, alias="SKYN3T_MASTER_KEY")
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
        return v

    @field_validator("data_dir", "logs_dir", "secret_storage_path", "audit_log_dir", "policy_file", mode="before")
    @classmethod
    def parse_paths(cls, v: Any) -> Any:
        if v is None:
            return None
        return Path(v)

    def ensure_directories(self) -> None:
        """Create necessary data directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_dir.mkdir(parents=True, exist_ok=True)
        Path(self.vector_db_path).mkdir(parents=True, exist_ok=True)


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
