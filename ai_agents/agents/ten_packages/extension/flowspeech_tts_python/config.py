import copy
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ten_ai_base import utils
from ten_ai_base.tts2_http import AsyncTTS2HttpConfig

DEFAULT_BASE_URL = "https://flowspeech.io"
JOBS_PATH = "/api/v1/tts/jobs"


def secure_url(value: str) -> httpx.URL:
    url = httpx.URL(value)
    if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
        raise ValueError(
            "FlowSpeech requires an HTTPS URL without credentials or fragment"
        )
    return url


class FlowSpeechParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    api_key: str = Field(min_length=1)
    base_url: str = DEFAULT_BASE_URL
    voice_name: str = Field(min_length=1, max_length=100)

    @field_validator("api_key")
    @classmethod
    def check_key(cls, value: str) -> str:
        if not value.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError("FlowSpeech api_key must be a nonempty token")
        return value

    @field_validator("voice_name")
    @classmethod
    def check_voice(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("FlowSpeech voice_name must not be blank")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def check_base(cls, value: str) -> str:
        url = secure_url(value)
        if url.query or url.path.rstrip("/") not in ("", JOBS_PATH):
            raise ValueError(
                "FlowSpeech base_url must be an origin or /api/v1/tts/jobs"
            )
        return str(url.copy_with(path=JOBS_PATH))


class FlowSpeechTTSConfig(AsyncTTS2HttpConfig):
    """FlowSpeech public API v1 job configuration."""

    model_config = ConfigDict(hide_input_in_errors=True)

    dump: bool = Field(default=False, description="Dump generated PCM audio")
    dump_path: str = Field(default="/tmp", description="PCM dump directory")
    params: dict[str, Any] = Field(default_factory=dict)
    total_timeout: float = Field(
        default=600.0, gt=0, le=7200, allow_inf_nan=False, strict=True
    )
    max_retries: int = Field(default=2, ge=0, le=10, strict=True)

    def resolved_params(self) -> FlowSpeechParams:
        try:
            return FlowSpeechParams.model_validate(self.params)
        except (ValueError, httpx.InvalidURL):
            pass
        # Raise outside the handler so no input-bearing validation error is chained.
        raise ValueError(
            "Invalid FlowSpeech params: check api_key, base_url and voice_name"
        )

    def update_params(self) -> None:
        self.validate()

    def validate(self) -> None:
        self.resolved_params()

    def to_str(self, sensitive_handling: bool = True) -> str:
        if not sensitive_handling:
            return f"{self}"
        config = copy.deepcopy(self)
        if config.params.get("api_key"):
            config.params["api_key"] = utils.encrypt(config.params["api_key"])
        return f"{config}"
