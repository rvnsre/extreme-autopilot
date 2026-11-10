from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from autopilot.desktop import default_apps, default_data_dir


class ApiCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    origin: str
    token: SecretStr

    @field_validator("origin")
    @classmethod
    def origin_only(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Credential origin must be an HTTPS origin without path, query or credentials")
        _ = url.port
        return f"https://{url.netloc.lower()}"

    @field_validator("token")
    @classmethod
    def bearer_format(cls, value):
        raw = value.get_secret_value()
        if not raw or not raw.isascii() or any(c.isspace() for c in raw):
            raise ValueError("Credential token must be nonempty ASCII without whitespace")
        return value


def token_configured(token: SecretStr):
    value = token.get_secret_value()
    return (
        len(value) >= 32
        and value.isascii()
        and not any(c.isspace() for c in value)
        and not value.startswith(("REPLACE_", "YOUR_"))
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EA_", env_file=".env", extra="ignore", allow_inf_nan=False, hide_input_in_errors=True
    )

    data_dir: Path = Field(default_factory=default_data_dir)
    api_token: SecretStr = SecretStr("")
    worker_token: SecretStr = SecretStr("")
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
    economy_enabled: bool = True
    economy_text_limit: int = Field(default=2400, ge=800, le=12000)
    economy_plan_ttl: int = Field(default=120, ge=1, le=3600)
    economy_plan_tokens: int = Field(default=1024, ge=256, le=16384)
    economy_decision_tokens: int = Field(default=1024, ge=256, le=16384)
    economy_verdict_tokens: int = Field(default=512, ge=256, le=16384)
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
    desktop_apps: dict[str, str] = Field(default_factory=default_apps)
    tick_seconds: float = Field(default=1, gt=0, le=30)
    ui_origins: list[str] = ["http://127.0.0.1:8765", "http://localhost:8765"]

    @model_validator(mode="after")
    def validate_endpoints(self):
        local = urlsplit(self.ollama_url)
        if local.scheme != "http" or local.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama must be a loopback HTTP service on the coordinator")
        if local.username or local.password or local.query or local.fragment or local.path not in {"", "/"}:
            raise ValueError("Ollama URL must be an origin without credentials, path or query")
        _ = local.port
        remote = urlsplit(self.server_url)
        if remote.scheme != "https" or not remote.hostname or remote.username or remote.password:
            raise ValueError("Worker server_url must use authenticated HTTPS, without URL credentials")
        if remote.query or remote.fragment or remote.path not in {"", "/"}:
            raise ValueError("Worker URL must be an origin without path or query")
        _ = remote.port
        self.ollama_url = self.ollama_url.rstrip("/")
        self.server_url = self.server_url.rstrip("/")
        return self

    def require_tokens(self):
        a, w = self.api_token.get_secret_value(), self.worker_token.get_secret_value()
        if not token_configured(self.api_token) or not token_configured(self.worker_token) or a == w:
            raise ValueError("Set different EA_API_TOKEN and EA_WORKER_TOKEN, each at least 32 characters")
