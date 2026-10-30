from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZERO_", env_file=".env", extra="ignore")
    home: Path = Path("data")
    host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    memory_backend: Literal["sqlite", "everos"] = "sqlite"
    everos_base_url: str = "http://127.0.0.1:8000"
    llm_provider: Literal["none", "ollama", "openai"] = "none"
    llm_model: str = ""
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_api_key: str = Field(default="", repr=False)
    embedding_model: str = ""
    allow_cloud: bool = False
    observatory_enabled: bool = True
    activity_enabled: bool = True
    max_upload_bytes: int = 64 * 1024 * 1024
    max_expanded_bytes: int = 256 * 1024 * 1024
    max_member_bytes: int = 128 * 1024 * 1024

    @field_validator("home")
    @classmethod
    def absolute_home(cls, value: Path) -> Path:
        return value.expanduser().resolve()


def is_loopback_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and parts.hostname in {"127.0.0.1", "::1", "localhost"}
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and bool(parts.port or parts.scheme)
        )
    except ValueError:
        return False
