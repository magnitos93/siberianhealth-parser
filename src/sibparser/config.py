"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Read from .env in the working directory."""

    model_config = SettingsConfigDict(
        env_prefix="SIBPARSER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_credentials: Path = Field(default=Path("credentials.json"))
    google_token: Path = Field(default=Path("token.json"))
    drive_root_folder: str = Field(default="SiberianHealthParser")

    downloads_dir: Path = Field(default=Path("downloads"))
    state_db: Path = Field(default=Path("state.db"))

    headful: bool = Field(default=True)
    request_delay: float = Field(default=0.5)

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return process-wide cached Settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    return _settings
