import copy
from typing import Any

from pydantic import Field
from ten_ai_base import utils
from ten_ai_base.tts2_http import AsyncTTS2HttpConfig


class FlowSpeechTTSConfig(AsyncTTS2HttpConfig):
    """Configuration for the FlowSpeech HTTP text-to-speech API."""

    dump: bool = Field(default=False, description="Dump generated PCM audio")
    dump_path: str = Field(default="/tmp", description="PCM dump directory")
    params: dict[str, Any] = Field(default_factory=dict)

    def update_params(self) -> None:
        self.params.pop("text", None)
        self.params.pop("originalText", None)
        self.params.pop("speakers", None)
        self.params.pop("sample_rate", None)

    def validate(self) -> None:
        if not self.params.get("api_key"):
            raise ValueError("API key is required for FlowSpeech TTS")
        if not self.params.get("voice_name"):
            raise ValueError("voice_name is required for FlowSpeech TTS")

    def to_str(self, sensitive_handling: bool = True) -> str:
        if not sensitive_handling:
            return f"{self}"

        config = copy.deepcopy(self)
        if config.params.get("api_key"):
            config.params["api_key"] = utils.encrypt(config.params["api_key"])
        return f"{config}"
