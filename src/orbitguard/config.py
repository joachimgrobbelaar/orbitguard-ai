"""Application configuration and environment settings for OrbitGuard AI."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "OrbitGuard AI"
    VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Server Network
    HOST: str = "0.0.0.0"
    PORT: int = 8045

    # Database Configuration (Supports SQLite local async or PostgreSQL/Supabase)
    DATABASE_URL: str = "sqlite+aiosqlite:///./orbitguard.db"

    # Redis Partitioning (Homelab standard: DB 3 reserved for OrbitGuard background queues)
    REDIS_URL: str = "redis://127.0.0.1:6379/3"

    # Space Data Feeds
    CELESTRAK_BASE_URL: str = "https://celestrak.org/NORAD/elements/gp.php"
    SPACETRACK_USER: str = ""
    SPACETRACK_PASSWORD: str = ""

    # Conjunction & Astrodynamics Thresholds
    CONJUNCTION_THRESHOLD_KM: float = 10.0      # Screening trigger distance
    CRITICAL_MISS_DISTANCE_KM: float = 2.0      # Immediate critical flag
    COLLISION_PROB_THRESHOLD: float = 1e-4      # 1 in 10,000 threshold for maneuver
    SCREENING_WINDOW_HOURS: int = 72            # 3-day projection window
    DEFAULT_STEP_SIZE_SECONDS: float = 60.0     # Time discretization for initial pass
    FINE_STEP_SIZE_SECONDS: float = 1.0         # Fine minimization window

    # Multi-Channel Alerting Settings
    TELEGRAM_BOT_TOKEN: str = ""
    DEFAULT_TELEGRAM_CHAT_ID: str = "6491601588"
    WEBHOOK_SECRET: str = "orbitguard_dev_secret_key"

    # LLM Briefing Settings (Ollama local / Mock / OpenAI)
    LLM_PROVIDER: str = "ollama"
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "deepseek-r1:7b"
    OPENAI_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
