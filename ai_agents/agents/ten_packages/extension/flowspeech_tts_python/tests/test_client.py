import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from flowspeech_tts_python.config import FlowSpeechTTSConfig
from flowspeech_tts_python.flowspeech_tts import FlowSpeechTTSClient
from ten_ai_base.struct import TTS2HttpResponseEventType


class FakeTenEnv:
    def log_error(self, *_args, **_kwargs):
        pass


@pytest.mark.asyncio
async def test_get_returns_pcm_chunks(monkeypatch):
    pcm = b"\x01\x02" * 3000
    config = FlowSpeechTTSConfig(params={"api_key": "test-key", "voice_name": "Kore"})
    client = FlowSpeechTTSClient(config, FakeTenEnv())
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://flowspeech.io"),
        json={
            "code": 0,
            "data": {
                "audioBase64": base64.b64encode(pcm).decode(),
                "sampleRate": 24000,
                "numChannels": 1,
                "bitsPerSample": 16,
            },
        },
    )
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(client._client, "post", post)

    events = [event async for event in client.get("hello", "req-1")]

    assert b"".join(data for data, kind in events if data) == pcm
    assert events[-1][1] == TTS2HttpResponseEventType.END
    request = post.call_args
    assert request.kwargs["headers"] == {"Authorization": "Bearer test-key"}
    assert request.kwargs["json"]["speakers"] == [{"voiceName": "Kore"}]
    await client.clean()


@pytest.mark.asyncio
async def test_get_classifies_invalid_key(monkeypatch):
    config = FlowSpeechTTSConfig(params={"api_key": "bad-key", "voice_name": "Kore"})
    client = FlowSpeechTTSClient(config, FakeTenEnv())
    response = httpx.Response(
        401, request=httpx.Request("POST", "https://flowspeech.io")
    )
    monkeypatch.setattr(client._client, "post", AsyncMock(return_value=response))

    events = [event async for event in client.get("hello", "req-2")]

    assert events[0][1] == TTS2HttpResponseEventType.INVALID_KEY_ERROR
    await client.clean()
