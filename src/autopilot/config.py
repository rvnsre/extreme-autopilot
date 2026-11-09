import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiCredential(BaseModel):
    origin: str
    token: SecretStr


def token_configured(token: SecretStr):
    value = token.get_secret_value()
    return len(value) >= 32 and not value.startswith(("REPLACE_", "YOUR_"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    data_dir: Path = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "ExtremeAutopilot"
    )
    api_token: SecretStr = SecretStr("")
    worker_token: SecretStr = SecretStr("")
    worker_id: str = "windows-01"
    server_url: str = "https://localhost:8766"
    ca_file: str | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    local_model: str = ""
    openai_key: SecretStr = SecretStr("")
    cloud_model: str = ""
    # Required explicit pricing for the selected model: no guessed or silently stale billing rates.
    input_usd_per_million: float = Field(default=0, ge=0)
    output_usd_per_million: float = Field(default=0, ge=0)
    cloud_output_tokens: int = Field(default=2048, ge=256, le=16384)
    image_token_reserve: int = Field(default=8192, ge=1024, le=65536)
    max_steps: int = Field(default=2000, ge=1, le=10000)
    headless: bool = False
    browser_channel: str | None = None
    browser_profile_template: Path | None = None
    api_credentials: dict[str, ApiCredential] = {}
    api_profile_origins: dict[str, str] = {}
    # Worker startup must explicitly attest that this is an isolated, dedicated machine/session.
    isolated_worker: bool = False
    process_commands: list[str] = ["python", "node", "git", "npm"]
    desktop_apps: dict[str, str] = {"notepad": "notepad.exe"}
    tick_seconds: float = Field(default=1, gt=0, le=30)
    ui_origins: list[str] = ["http://127.0.0.1:8765", "http://localhost:8765"]

    @model_validator(mode="after")
    def validate_endpoints(self):
        local = urlsplit(self.ollama_url)
        if local.scheme != "http" or local.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama must be a loopback HTTP service on the coordinator")
        remote = urlsplit(self.server_url)
        if remote.scheme != "https" or not remote.hostname or remote.username or remote.password:
            raise ValueError("Worker server_url must use authenticated HTTPS, without URL credentials")
        return self

    def require_tokens(self):
        a, w = self.api_token.get_secret_value(), self.worker_token.get_secret_value()
        if not token_configured(self.api_token) or not token_configured(self.worker_token) or a == w:
            raise ValueError("Set different EA_API_TOKEN and EA_WORKER_TOKEN, each at least 32 characters")
