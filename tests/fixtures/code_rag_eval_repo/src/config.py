"""Application configuration.

Configuration is loaded from environment variables with fallback defaults.
DEBUG mode enables verbose logging and bypasses rate limits.
"""
import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class AppConfig:
    """Main application configuration container."""

    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    db_path: str = "app.db"
    max_connections: int = 10
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", "insecure-dev-key"))
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_per_minute: int = 60

    def is_production(self) -> bool:
        return not self.debug and self.log_level != "DEBUG"


def load_config() -> AppConfig:
    """Load configuration from environment variables."""
    return AppConfig(
        debug=os.environ.get("DEBUG", "").lower() in ("1", "true"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        db_path=os.environ.get("DB_PATH", "app.db"),
        max_connections=int(os.environ.get("MAX_CONNECTIONS", "10")),
        secret_key=os.environ.get("SECRET_KEY", "insecure-dev-key"),
        allowed_origins=os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
        rate_limit_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")),
    )
