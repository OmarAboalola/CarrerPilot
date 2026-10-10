"""Validated local-demo settings; credentials never leave this module."""

import asyncio
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    _model_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _model_cooldown_until: float = PrivateAttr(default=0)
    _model_version: str = PrivateAttr(default="environment")
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    mode: Literal["fixture", "live"] = "fixture"
    data_dir: Path = ROOT / "data"
    base_url: str = "http://127.0.0.1:8765"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    greenhouse_boards: str = "assemblyai"
    jsearch_api_key: SecretStr = SecretStr("")
    jsearch_enabled: bool = False
    jsearch_monthly_budget: int = Field(default=180, ge=1, le=190)
    request_timeout: float = Field(default=20, gt=0, le=60)
    scan_timeout: int = Field(default=120, ge=10, le=300)
    max_jobs: int = Field(default=50, ge=1, le=100)
    max_threads: int = Field(default=30, ge=1, le=50)

    @field_validator("base_url")
    @classmethod
    def local_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("This build supports loopback-only local use.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid application URL.")
        return value.rstrip("/")

    @field_validator("llm_base_url")
    @classmethod
    def model_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("LLM endpoint must be an explicitly configured HTTPS origin.")
        return value.rstrip("/")

    @field_validator("greenhouse_boards")
    @classmethod
    def boards(cls, value: str) -> str:
        import re

        tokens = [token.strip() for token in value.split(",") if token.strip()]
        if not 1 <= len(tokens) <= 5 or any(
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", token) for token in tokens
        ):
            raise ValueError("Configure between one and five valid observed Greenhouse board tokens.")
        return ",".join(tokens)

    @property
    def model_ready(self) -> bool:
        return bool(self.llm_api_key.get_secret_value() and self.llm_model)

    @property
    def gmail_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret.get_secret_value())
