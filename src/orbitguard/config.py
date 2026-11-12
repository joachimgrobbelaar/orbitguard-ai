"""Runtime settings. Secrets come from the environment and are never defaulted."""

from __future__ import annotations

import os


class Settings:
    PROJECT_NAME: str = "OrbitGuard"
    VERSION: str = "0.2.0"
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "7860"))
    DEBUG: bool = os.getenv("DEBUG", "").lower() in {"1", "true", "yes"}


settings = Settings()
