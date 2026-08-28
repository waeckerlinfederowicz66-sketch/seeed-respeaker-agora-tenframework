import base64
from collections.abc import AsyncIterator
from typing import Any

import httpx
from ten_ai_base.const import LOG_CATEGORY_VENDOR
from ten_ai_base.struct import TTS2HttpResponseEventType
from ten_ai_base.tts2_http import AsyncTTS2HttpClient
from ten_runtime import AsyncTenEnv

from .config import FlowSpeechTTSConfig

BYTES_PER_SAMPLE = 2
DEFAULT_BASE_URL = "https://flowspeech.io"
DEFAULT_CHUNK_SIZE = 4096


class FlowSpeechTTSClient(AsyncTTS2HttpClient):
    def __init__(self, config: FlowSpeechTTSConfig, ten_env: AsyncTenEnv) -> None:
        super().__init__()
        self.config = config
        self.ten_env = ten_env
        self._is_cancelled = False
        self._client = httpx.AsyncClient(timeout=90.0)

    async def cancel(self) -> None:
        self._is_cancelled = True

    async def get(
        self, text: str, request_id: str
    ) -> AsyncIterator[tuple[bytes | None, TTS2HttpResponseEventType]]:
        self._is_cancelled = False
        if not text.strip():
            yield None, TTS2HttpResponseEventType.END
            return

        params = dict(self.config.params)
        api_key = params.pop("api_key")
        base_url = str(params.pop("base_url", DEFAULT_BASE_URL)).rstrip("/")
        voice_name = params.pop("voice_name")
        body: dict[str, Any] = {
            "text": text,
            "originalText": text,
            "speakers": [{"voiceName": voice_name}],
        }
        body.update(params)

        try:
            response = await self._client.post(
                f"{base_url}/api/ai/text-to-speech",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            if response.status_code in (401, 403):
                yield (
                    b"FlowSpeech authentication failed",
                    TTS2HttpResponseEventType.INVALID_KEY_ERROR,
                )
                return

            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not data.get("audioBase64"):
                raise RuntimeError("FlowSpeech response did not include audio")

            sample_rate = int(data.get("sampleRate", 24000))
            channels = int(data.get("numChannels", 1))
            bits_per_sample = int(data.get("bitsPerSample", 16))
            if (sample_rate, channels, bits_per_sample) != (24000, 1, 16):
                raise RuntimeError(
                    "FlowSpeech returned an unsupported audio format; "
                    "expected 24 kHz mono 16-bit PCM"
                )

            audio = base64.b64decode(data["audioBase64"], validate=True)
            chunk_size = DEFAULT_CHUNK_SIZE - (DEFAULT_CHUNK_SIZE % BYTES_PER_SAMPLE)
            for offset in range(0, len(audio), chunk_size):
                if self._is_cancelled:
                    yield None, TTS2HttpResponseEventType.FLUSH
                    return
                chunk = audio[offset : offset + chunk_size]
                if len(chunk) % BYTES_PER_SAMPLE:
                    raise RuntimeError("FlowSpeech returned unaligned PCM audio")
                if chunk:
                    yield chunk, TTS2HttpResponseEventType.RESPONSE

            yield None, TTS2HttpResponseEventType.END
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            self.ten_env.log_error(
                f"vendor_error: {exc} of request_id: {request_id}",
                category=LOG_CATEGORY_VENDOR,
            )
            yield str(exc).encode("utf-8"), TTS2HttpResponseEventType.ERROR

    async def clean(self) -> None:
        await self._client.aclose()

    def get_extra_metadata(self) -> dict[str, Any]:
        return {"voice": self.config.params.get("voice_name", "")}
