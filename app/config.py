from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Amnezia Service"
    environment: str = "development"
    base_url: str = "http://localhost:8000"
    secret_key: str = "dev-only-change-me-please"
    encryption_key: str | None = None
    database_url: str = "sqlite:///./amnezia-service.db"
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])

    admin_email: str = "admin@example.com"
    admin_password: str = "admin123456"

    payment_provider: str = "mock"
    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None

    vpn_backend: str = "mock"
    awg_binary: str = "awg"
    awg_quick_binary: str = "awg-quick"
    awg_interface: str = "awg0"
    awg_config_path: Path = Path("/etc/amnezia/amneziawg/awg0.conf")
    awg_endpoint: str = "vpn.example.com:51820"
    awg_subnet: str = "10.8.1.0/24"
    awg_dns: str = "1.1.1.1, 1.0.0.1"
    awg_command_prefix: list[str] = Field(default_factory=list)
    awg_save_config: bool = True
    awg_i1: str | None = None
    awg_i2: str | None = None
    awg_i3: str | None = None
    awg_i4: str | None = None
    awg_i5: str | None = None

    session_https_only: bool = False
    subscription_reconcile_seconds: int = 60
    max_devices_per_subscription: int = 20

    @field_validator("trusted_hosts", "awg_command_prefix", mode="before")
    @classmethod
    def parse_json_lists(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.environment == "production":
            if self.secret_key == "dev-only-change-me-please" or len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be changed and contain at least 32 characters")
            if self.admin_password == "admin123456" or len(self.admin_password) < 12:
                raise ValueError("ADMIN_PASSWORD must be changed and contain at least 12 characters")
            if not self.base_url.startswith("https://"):
                raise ValueError("BASE_URL must use HTTPS in production")
            if not self.session_https_only:
                raise ValueError("SESSION_HTTPS_ONLY must be true in production")
        if self.payment_provider == "yookassa" and not (
            self.yookassa_shop_id and self.yookassa_secret_key
        ):
            raise ValueError("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY are required")
        return self

    @property
    def fernet_key(self) -> bytes:
        if self.encryption_key:
            raw = self.encryption_key.encode()
            try:
                decoded = base64.urlsafe_b64decode(raw)
            except Exception as exc:  # pragma: no cover - pydantic surfaces this
                raise ValueError("ENCRYPTION_KEY must be a Fernet key") from exc
            if len(decoded) != 32:
                raise ValueError("ENCRYPTION_KEY must decode to 32 bytes")
            return raw
        digest = hashlib.sha256(("vpn-config:" + self.secret_key).encode()).digest()
        return base64.urlsafe_b64encode(digest)


@lru_cache
def get_settings() -> Settings:
    return Settings()
