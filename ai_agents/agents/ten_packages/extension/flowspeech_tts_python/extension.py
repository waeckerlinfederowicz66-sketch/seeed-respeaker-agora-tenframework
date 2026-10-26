#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from ten_ai_base.tts2_http import (
    AsyncTTS2HttpClient,
    AsyncTTS2HttpConfig,
    AsyncTTS2HttpExtension,
)
from ten_runtime import AsyncTenEnv

from .config import FlowSpeechTTSConfig
from .flowspeech_tts import FlowSpeechTTSClient


class FlowSpeechTTSExtension(AsyncTTS2HttpExtension):
    """Download public API v1 WAV recordings and emit 24 kHz mono PCM."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: FlowSpeechTTSConfig | None = None
        self.client: FlowSpeechTTSClient | None = None

    async def create_config(self, config_json_str: str) -> AsyncTTS2HttpConfig:
        return FlowSpeechTTSConfig.model_validate_json(config_json_str)

    async def create_client(
        self, config: AsyncTTS2HttpConfig, ten_env: AsyncTenEnv
    ) -> AsyncTTS2HttpClient:
        return FlowSpeechTTSClient(config=config, ten_env=ten_env)

    def vendor(self) -> str:
        return "flowspeech"

    def synthesize_audio_sample_rate(self) -> int:
        return 24000
