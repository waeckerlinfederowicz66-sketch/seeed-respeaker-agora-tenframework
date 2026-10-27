import asyncio
import io
import json
import struct
import traceback
import wave
from email.utils import formatdate
from uuid import UUID, uuid4

import httpx
import pytest
from flowspeech_tts_python.config import FlowSpeechTTSConfig
from flowspeech_tts_python.flowspeech_tts import (
    FlowSpeechTTSClient,
    wav_to_pcm,
    FlowSpeechError,
)
from ten_ai_base.struct import TTS2HttpResponseEventType as Event

JOB_ID = "11111111-2222-4333-8444-555555555555"
REQUEST_ID = "b698824b-de05-486d-8833-05d5c4522a39"
AUDIO_URL = "https://audio.example.com/result.wav"
PCM = b"\x01\x02" * 3000


@pytest.fixture(autouse=True)
def deterministic_request_id(monkeypatch):
    monkeypatch.setattr(
        "flowspeech_tts_python.flowspeech_tts.uuid4", lambda: UUID(REQUEST_ID)
    )


def wav_bytes(pcm=PCM, rate=24000, channels=1, width=2, metadata=False):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    data = output.getvalue()
    if metadata:
        data = data[:12] + b"JUNK" + struct.pack("<I", 12) + b"metadata1234" + data[12:]
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
    return data


def resized_riff(content):
    return content[:4] + struct.pack("<I", len(content) - 8) + content[8:]


def changed_wav(offset, replacement):
    content = wav_bytes()
    return content[:offset] + replacement + content[offset + len(replacement) :]


def job(status="success", **overrides):
    return {
        "id": JOB_ID,
        "requestId": REQUEST_ID,
        "status": status,
        "usage": {
            "billableCharacters": 5,
            "reservedCredits": 5
            if status in ("queued", "running", "merging", "settling")
            else 0,
            "chargedCredits": 5 if status == "success" else 0,
        },
        "result": {
            "url": AUDIO_URL,
            "mimeType": "audio/wav",
            "sampleRate": 24000,
            "channels": 1,
            "bitsPerSample": 16,
        }
        if status == "success"
        else None,
        **overrides,
    }


class FakeTenEnv:
    def __init__(self):
        self.errors = []

    def log_error(self, message, **kwargs):
        self.errors.append(message)


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        self.waits.append(delay)
        await asyncio.sleep(0)


def config(**kwargs):
    return FlowSpeechTTSConfig(
        params={"api_key": "test-key", "voice_name": "Kore"}, **kwargs
    )


async def collect(respond, *, settings=None, clock=None, text="hello"):
    clock = clock or Clock()
    env = FakeTenEnv()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "discard", "X-Client-Secret": "discard"},
        cookies={"session": "discard"},
        auth=("discard", "discard"),
        follow_redirects=True,
    ) as transport:
        client = FlowSpeechTTSClient(
            settings or config(),
            env,
            http_client=transport,
            clock=clock.time,
            sleep=clock.sleep,
            wall_clock=lambda: 0,
        )
        events = [event async for event in client.get(text, "ten-correlation-id")]
        metadata = client.get_extra_metadata()
        await client.clean()
        assert not transport.is_closed
    return events, metadata, env


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize(
    "request_id",
    [None, 7, "invalid", REQUEST_ID.replace("-", ""), "{" + REQUEST_ID + "}", JOB_ID],
)
async def test_invalid_request_identity_never_downloads(poll, request_id):
    calls = []
    responses = iter(([job("queued")] if poll else []) + [job(requestId=request_id)])

    def respond(request):
        calls.append(request)
        assert request.url.host == "flowspeech.io"
        return httpx.Response(200, json=next(responses))

    events, metadata, _ = await collect(respond)
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert metadata["api_request_id"] == REQUEST_ID
    assert len(calls) == (2 if poll else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize(
    "status",
    ["queued", "running", "merging", "settling", "success", "failed", "cancelled"],
)
async def test_identity_checked_for_every_job_state(poll, status):
    responses = iter(
        ([job("queued")] if poll else []) + [job(status, requestId=JOB_ID)]
    )
    events, _, _ = await collect(
        lambda request: httpx.Response(200, json=next(responses))
    )
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert b"different request ID" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize("field", ["requestId", "usage"])
async def test_job_requires_identity_and_usage(poll, field):
    payload = {key: value for key, value in job().items() if key != field}
    responses = iter(([job("queued")] if poll else []) + [payload])
    events, _, _ = await collect(
        lambda request: httpx.Response(200, json=next(responses))
    )
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert b"invalid job" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
@pytest.mark.parametrize(
    "field", ["billableCharacters", "reservedCredits", "chargedCredits"]
)
@pytest.mark.parametrize(
    "value", [None, -1, True, "2", 1.5, float("nan"), float("inf")]
)
async def test_invalid_usage_never_downloads(poll, field, value):
    payload = job(usage={**job()["usage"], field: value})
    responses = iter(([job("queued")] if poll else []) + [payload])

    def respond(request):
        assert request.url.host == "flowspeech.io"
        return httpx.Response(200, content=json.dumps(next(responses)))

    events, _, _ = await collect(respond)
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert b"invalid job" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage", [None, {}, {"billableCharacters": 5, "reservedCredits": 0}]
)
async def test_missing_usage_counters(usage):
    events, _, _ = await collect(
        lambda request: httpx.Response(200, json=job(usage=usage))
    )
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert b"invalid job" in events[0][0]


@pytest.mark.asyncio
async def test_response_identity_normalizes_uppercase_uuid():
    mixed_case_id = "aa111111-2222-4333-8444-555555555555"
    responses = iter(
        [
            job("queued", requestId=REQUEST_ID.upper(), id=mixed_case_id.upper()),
            job(requestId=REQUEST_ID.upper(), id=mixed_case_id),
        ]
    )

    def respond(request):
        if request.url.host == "audio.example.com":
            return httpx.Response(200, content=wav_bytes())
        return httpx.Response(200, json=next(responses))

    events, metadata, _ = await collect(respond)
    assert events[-1][1] == Event.END
    assert metadata["job_id"] == mixed_case_id
    assert metadata["api_request_id"] == REQUEST_ID


@pytest.mark.asyncio
async def test_pending_jobs_download_wav_then_emit_only_pcm():
    clock = Clock()
    requests = []
    states = iter(["queued", "running", "merging", "settling", "success"])

    def respond(request):
        requests.append(request)
        assert "cookie" not in request.headers
        assert "x-client-secret" not in request.headers
        if request.url.host == "audio.example.com":
            assert "authorization" not in request.headers
            assert "idempotency-key" not in request.headers
            return httpx.Response(200, content=wav_bytes(metadata=True))
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "POST":
            assert UUID(request.headers["idempotency-key"]).version == 4
            assert json.loads(request.content) == {
                "input": {"mode": "single", "text": "hello", "voiceName": "Kore"}
            }
            assert request.url.path == "/api/v1/tts/jobs"
        else:
            assert request.url.path == f"/api/v1/tts/jobs/{JOB_ID}"
        return httpx.Response(
            202 if request.method == "POST" else 200,
            json=job(next(states)),
            headers={"set-cookie": "session=discard"},
        )

    events, metadata, env = await collect(respond, clock=clock)
    chunks = [data for data, kind in events if kind == Event.RESPONSE]
    assert b"".join(chunks) == PCM
    assert [len(chunk) for chunk in chunks] == [4096, 1904]
    assert all(len(chunk) % 2 == 0 for chunk in chunks)
    assert events[-1] == (None, Event.END)
    assert clock.waits == [5, 5, 5, 5]
    assert metadata["job_id"] == JOB_ID
    assert metadata["api_request_id"] == requests[0].headers["idempotency-key"]
    assert len(requests) == 6
    assert env.errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "server", "rate"])
async def test_submit_retries_same_body_and_uuid(failure):
    requests = []
    clock = Clock()

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "network":
                raise httpx.ReadTimeout("lost response")
            return httpx.Response(
                429 if failure == "rate" else 503,
                json={"error": {"code": "busy", "message": "wait", "retryable": True}},
                headers={"Retry-After": "12"},
            )
        return (
            httpx.Response(200, json=job())
            if request.method == "POST"
            else httpx.Response(200, content=wav_bytes())
        )

    events, _, _ = await collect(respond, clock=clock)
    assert events[-1][1] == Event.END
    assert requests[0].content == requests[1].content
    assert (
        requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    )
    assert clock.waits == [5 if failure == "network" else 12]


@pytest.mark.asyncio
async def test_http_date_retry_after_and_poll_rate_limit():
    clock = Clock()
    responses = iter(
        [
            httpx.Response(
                202,
                json=job("queued"),
                headers={"Retry-After": "Thu, 01 Jan 1970 00:00:12 GMT"},
            ),
            httpx.Response(429, headers={"Retry-After": "9"}),
            httpx.Response(200, json=job()),
            httpx.Response(200, content=wav_bytes()),
        ]
    )
    events, _, _ = await collect(lambda request: next(responses), clock=clock)
    assert events[-1][1] == Event.END
    assert clock.waits == [12, 9]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_job_failure_is_not_http_success(status):
    events, metadata, env = await collect(
        lambda request: httpx.Response(
            200,
            json=job(
                status,
                error={
                    "code": "stopped",
                    "message": "cannot finish",
                    "retryable": False,
                },
            ),
        )
    )
    assert len(events) == 1 and events[0][1] == Event.ERROR
    assert status.encode() in events[0][0] and b"upstream_error" in events[0][0]
    assert JOB_ID in env.errors[0]
    assert metadata["api_request_id"] in env.errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        (job(result=None, resultDeleted=True), b"deleted"),
        (job(result=None), b"no result"),
        (job(status="unknown"), b"invalid job"),
        (job(id="../escape"), b"invalid job"),
        ({"status": "queued"}, b"invalid job"),
        (job(result={"url": AUDIO_URL}), b"invalid job"),
        ({"code": 0, "data": {"audioBase64": "old"}}, b"invalid job"),
    ],
)
async def test_invalid_job(payload, expected):
    events, _, _ = await collect(lambda request: httpx.Response(200, json=payload))
    assert events[0][1] == Event.ERROR and expected in events[0][0]


@pytest.mark.asyncio
async def test_job_id_cannot_change():
    responses = iter(
        [
            httpx.Response(202, json=job("queued")),
            httpx.Response(200, json=job(id="b698824b-de05-486d-8833-05d5c4522a39")),
        ]
    )
    events, _, _ = await collect(lambda request: next(responses))
    assert events[0][1] == Event.ERROR and b"different job ID" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://audio.example.com/a",
        "https://u:p@audio.example.com/a",
        "file:///tmp/audio",
        "https://audio.example.com/a#x",
    ],
)
async def test_unsafe_audio_url(url):
    events, _, _ = await collect(
        lambda request: httpx.Response(
            200, json=job(result={**job()["result"], "url": url})
        )
    )
    assert events[0][1] == Event.ERROR and b"unsafe audio URL" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("download", [False, True])
async def test_redirect_is_never_followed(download):
    calls = []

    def respond(request):
        calls.append(request)
        if download and len(calls) == 1:
            return httpx.Response(200, json=job())
        return httpx.Response(307, headers={"Location": "https://unexpected.example/"})

    events, _, _ = await collect(respond)
    assert events[0][1] == Event.ERROR and b"redirect" in events[0][0]
    assert len(calls) == (2 if download else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 413, 415, 503])
async def test_api_errors_and_authentication_event(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": "invalid_request",
                    "message": "message",
                    "retryable": False,
                },
                "traceId": JOB_ID,
            },
        )

    events, _, env = await collect(respond)
    assert events[0][1] == (
        Event.INVALID_KEY_ERROR if status in (401, 403) else Event.ERROR
    )
    assert b"invalid_request" in events[0][0] and JOB_ID.encode() in events[0][0]
    assert len(calls) == 1
    assert "test-key" not in env.errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 401, 403, 429, 503])
@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("failure", ["body", "close"])
@pytest.mark.parametrize("cooldown", ["60", formatdate(60, usegmt=True)])
async def test_interrupted_response_preserves_status_and_cooldown(
    status, download, failure, cooldown
):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x"
            if failure == "body":
                raise httpx.ReadError("private-body-sentinel")

        async def aclose(self):
            if failure == "close":
                raise httpx.ReadError("private-close-sentinel")

    broken = httpx.Response(
        status, headers={"Retry-After": cooldown}, stream=BrokenStream()
    )
    responses = (
        [
            httpx.Response(202, json=job()),
            broken,
            httpx.Response(200, content=wav_bytes()),
        ]
        if download
        else [
            broken,
            httpx.Response(202, json=job()),
            httpx.Response(200, content=wav_bytes()),
        ]
    )
    pending = iter(responses)
    calls = []

    def respond(request):
        calls.append(request)
        return next(pending)

    clock = Clock()
    events, metadata, env = await collect(
        respond, clock=clock, settings=config(max_retries=1)
    )
    if status in (401, 403):
        assert clock.waits == []
        assert len(calls) == (2 if download else 1)
        assert [kind for _, kind in events] == [
            Event.ERROR if download else Event.INVALID_KEY_ERROR
        ]
    else:
        assert clock.waits == [60]
        assert events[-1][1] == Event.END
        assert len(calls) == 3
        if not download:
            assert calls[0].content == calls[1].content
            assert (
                calls[0].headers["Idempotency-Key"]
                == calls[1].headers["Idempotency-Key"]
                == REQUEST_ID
            )
        else:
            assert all("authorization" not in request.headers for request in calls[1:])
    assert broken.is_closed
    assert metadata["api_request_id"] == REQUEST_ID
    assert "private-body-sentinel" not in repr(events) + repr(env.errors)
    assert "private-close-sentinel" not in repr(events) + repr(env.errors)


@pytest.mark.asyncio
async def test_interrupted_rate_limit_cooldown_exceeds_budget_without_retry():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("synthetic body failure")
            yield b""

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"}, stream=BrokenStream())

    clock = Clock()
    events, metadata, _ = await collect(
        respond, clock=clock, settings=config(total_timeout=10.0)
    )
    assert len(calls) == 1 and clock.waits == []
    assert [kind for _, kind in events] == [Event.ERROR]
    assert b"deadline" in events[0][0] and metadata["api_request_id"] == REQUEST_ID


@pytest.mark.asyncio
async def test_download_failures_retry_without_regenerating():
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=job())
        if len(calls) == 2:
            raise httpx.ReadError("truncated transport")
        return httpx.Response(200, content=wav_bytes())

    events, _, _ = await collect(respond)
    assert events[-1][1] == Event.END
    assert [request.method for request in calls] == ["POST", "GET", "GET"]
    assert calls[1].url == calls[2].url


@pytest.mark.asyncio
async def test_download_auth_failure_is_not_api_key_failure():
    responses = iter([httpx.Response(200, json=job()), httpx.Response(403)])
    events, _, _ = await collect(lambda request: next(responses))
    assert events[0][1] == Event.ERROR


@pytest.mark.asyncio
async def test_retry_exhaustion():
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("network")

    events, metadata, _ = await collect(respond, settings=config(max_retries=1))
    assert len(calls) == 2
    assert events[0][1] == Event.ERROR
    assert metadata["api_request_id"].encode() in events[0][0]


@pytest.mark.asyncio
async def test_deadline_rejects_long_wait():
    clock = Clock()
    events, _, _ = await collect(
        lambda request: httpx.Response(
            202, json=job("queued"), headers={"Retry-After": "30"}
        ),
        settings=config(total_timeout=10),
        clock=clock,
    )
    assert events[0][1] == Event.ERROR and b"deadline" in events[0][0]
    assert clock.waits == []


@pytest.mark.asyncio
async def test_total_deadline_interrupts_network():
    async def respond(request):
        await asyncio.sleep(1)
        return httpx.Response(202, json=job("queued"))

    events, _, _ = await collect(respond, settings=config(total_timeout=0.02))
    assert events[0][1] == Event.ERROR and b"deadline" in events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["network", "poll", "chunks"])
async def test_cancel_emits_flush(where):
    started = asyncio.Event()

    async def respond(request):
        if where == "network":
            started.set()
            await asyncio.sleep(100)
        if request.method == "POST":
            return httpx.Response(
                202, json=job("queued" if where == "poll" else "success")
            )
        return httpx.Response(200, content=wav_bytes())

    async def sleep(delay):
        started.set()
        await asyncio.sleep(100)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = FlowSpeechTTSClient(
            config(), FakeTenEnv(), http_client=transport, sleep=sleep
        )
        generator = client.get("hello", "ten-id")
        if where == "chunks":
            assert (await anext(generator))[1] == Event.RESPONSE
            await client.cancel()
            events = [event async for event in generator]
        else:
            task = asyncio.create_task(anext(generator))
            await started.wait()
            await client.cancel()
            events = [await asyncio.wait_for(task, 1)]
            await generator.aclose()
        assert events[-1] == (None, Event.FLUSH)
        await client.clean()


@pytest.mark.asyncio
async def test_external_task_cancellation_propagates():
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.sleep(100)
        return httpx.Response(200, json=job())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = FlowSpeechTTSClient(config(), FakeTenEnv(), http_client=transport)
        generator = client.get("hello", "ten-id")
        task = asyncio.create_task(anext(generator))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client._active
        await client.clean()


@pytest.mark.asyncio
async def test_repeated_ten_correlation_id_does_not_collapse_distinct_chunks(
    monkeypatch,
):
    monkeypatch.setattr("flowspeech_tts_python.flowspeech_tts.uuid4", uuid4)
    ids = []

    def respond(request):
        if request.method == "POST":
            ids.append(request.headers["idempotency-key"])
            return httpx.Response(200, json=job(requestId=ids[-1]))
        return httpx.Response(200, content=wav_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = FlowSpeechTTSClient(config(), FakeTenEnv(), http_client=transport)
        for text in ("one", "two"):
            assert [event async for event in client.get(text, "same-ten-id")][-1][
                1
            ] == Event.END
        await client.clean()
    assert len(ids) == len(set(ids)) == 2


@pytest.mark.asyncio
async def test_empty_input_no_network():
    def unexpected(request):
        pytest.fail("empty input must not call the API")

    events, _, _ = await collect(unexpected, text=" ")
    assert events == [(None, Event.END)]


@pytest.mark.parametrize(
    "audio",
    [
        b"not wav",
        wav_bytes()[:-2],
        wav_bytes(rate=48000),
        wav_bytes(channels=2),
        wav_bytes(width=1),
        wav_bytes(pcm=b""),
        wav_bytes(pcm=PCM + b"x"),
        changed_wav(4, struct.pack("<I", 9999)),
        changed_wav(34, struct.pack("<H", 9)),
        changed_wav(32, struct.pack("<H", 4)),
        changed_wav(28, struct.pack("<I", 24000)),
        resized_riff(wav_bytes() + b"x"),
        resized_riff(wav_bytes() + b"JUNK" + struct.pack("<I", 5)),
        resized_riff(wav_bytes(pcm=PCM + b"x") + b"\x00"),
        resized_riff(wav_bytes()[:-2]),
    ],
)
def test_invalid_wav_not_emitted(audio):
    with pytest.raises(FlowSpeechError):
        wav_to_pcm(audio)


def test_padded_metadata_chunk_does_not_become_pcm():
    audio = wav_bytes()
    with_metadata = resized_riff(
        audio[:12] + b"JUNK" + struct.pack("<I", 3) + b"abc\x00" + audio[12:]
    )
    assert wav_to_pcm(with_metadata) == PCM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": str(101 * 1024 * 1024)},
        {"Content-Length": "invalid"},
        {"Content-Encoding": "unknown"},
    ],
)
async def test_download_bounds(headers):
    responses = iter(
        [
            httpx.Response(200, json=job()),
            httpx.Response(200, content=wav_bytes(), headers=headers),
        ]
    )
    events, _, _ = await collect(lambda request: next(responses))
    assert events[0][1] == Event.ERROR


@pytest.mark.parametrize(
    "key",
    [
        "model",
        "source",
        "text",
        "originalText",
        "speakers",
        "sample_rate",
        "speed",
        "format",
        "segments",
    ],
)
def test_rejects_unsupported_params_instead_of_dropping(key):
    settings = FlowSpeechTTSConfig(
        params={"api_key": "test-key", "voice_name": "Kore", key: "unsupported"}
    )
    with pytest.raises(ValueError):
        settings.update_params()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"api_key": "", "voice_name": "Kore"},
        {"api_key": "two words", "voice_name": "Kore"},
        {"api_key": "test-key", "voice_name": " "},
        {"api_key": "test-key", "voice_name": "Kore", "base_url": "http://example.com"},
        {
            "api_key": "test-key",
            "voice_name": "Kore",
            "base_url": "https://u:p@example.com",
        },
        {
            "api_key": "test-key",
            "voice_name": "Kore",
            "base_url": "https://example.com/api/ai/text-to-speech",
        },
        {
            "api_key": "test-key",
            "voice_name": "Kore",
            "base_url": "https://example.com/?key=x",
        },
    ],
)
def test_config_validation(params):
    with pytest.raises(ValueError):
        FlowSpeechTTSConfig(params=params).validate()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_timeout": 0},
        {"total_timeout": float("inf")},
        {"total_timeout": True},
        {"max_retries": -1},
        {"max_retries": 11},
        {"max_retries": 1.5},
    ],
)
def test_deadline_retry_config(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_sensitive_config_masks_key():
    assert "test-key" not in config().to_str()
    assert "test-key" in config().to_str(sensitive_handling=False)


@pytest.mark.parametrize(
    "value", ["private-sentinel ", b"private-sentinel", {"token": "private-sentinel"}]
)
@pytest.mark.parametrize(
    "entry", ["validate", "update_params", "resolved_params", "client"]
)
def test_invalid_key_never_survives_public_validation(value, entry):
    settings = FlowSpeechTTSConfig(params={"api_key": value, "voice_name": "Kore"})
    with pytest.raises(ValueError) as caught:
        if entry == "client":
            FlowSpeechTTSClient(settings, FakeTenEnv())
        else:
            getattr(settings, entry)()
    assert "private-sentinel" not in str(caught.value)
    assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_invalid_url_diagnostic_does_not_reflect_configuration():
    settings = FlowSpeechTTSConfig(
        params={
            "api_key": "test-only",
            "voice_name": "Kore",
            "base_url": "https://example.test:private-sentinel",
        }
    )
    with pytest.raises(ValueError) as caught:
        settings.validate()
    assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["api", "job", "download"])
async def test_upstream_error_fields_never_reach_logs_or_events(where, caplog):
    marker = "private-sentinel"
    private_url = "https://audio.example.test/a?signature=private-link"
    details = {
        "code": marker,
        "message": marker + " " + private_url,
        "retryable": False,
    }
    calls = []
    settings = FlowSpeechTTSConfig(
        params={"api_key": marker, "voice_name": "Kore"}, max_retries=0
    )

    def respond(request):
        calls.append(request)
        if where == "job":
            return httpx.Response(200, json=job("failed", error=details))
        if where == "download" and request.method == "POST":
            return httpx.Response(202, json=job())
        return httpx.Response(
            403, json={"error": details, "traceId": marker + private_url}
        )

    events, metadata, env = await collect(respond, settings=settings)
    assert events[0][1] == (Event.INVALID_KEY_ERROR if where == "api" else Event.ERROR)
    rendered = repr(events) + repr(env.errors) + caplog.text
    assert marker not in rendered and "private-link" not in rendered
    assert REQUEST_ID in rendered and metadata["api_request_id"] == REQUEST_ID
    assert len(calls) == (2 if where == "download" else 1)
    if where == "download":
        assert "authorization" not in calls[-1].headers


@pytest.mark.asyncio
async def test_conversion_expiry_rejects_audio_and_success(monkeypatch):
    from flowspeech_tts_python import flowspeech_tts as module

    clock = Clock()
    original = module.wav_to_pcm
    responses = [
        httpx.Response(202, json=job()),
        httpx.Response(200, content=wav_bytes()),
    ]

    def conversion(content):
        pcm = original(content)
        clock.now = 2
        return pcm

    monkeypatch.setattr(module, "wav_to_pcm", conversion)
    pending = iter(responses)
    events, metadata, _ = await collect(
        lambda _: next(pending), settings=config(total_timeout=1.0), clock=clock
    )
    assert [kind for _, kind in events] == [Event.ERROR]
    assert b"deadline" in events[0][0]
    assert metadata["job_id"] == JOB_ID and metadata["api_request_id"] == REQUEST_ID
    assert all(response.is_closed for response in responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [10, 3000])
async def test_emission_expiry_stops_later_pcm_and_end(samples):
    clock = Clock()
    responses = iter(
        [
            httpx.Response(202, json=job()),
            httpx.Response(200, content=wav_bytes(b"\x01\x02" * samples)),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(responses))
    ) as http:
        client = FlowSpeechTTSClient(
            config(total_timeout=1.0), FakeTenEnv(), http_client=http, clock=clock.time
        )
        stream = client.get("Hello", "correlation")
        first = await anext(stream)
        assert first[1] == Event.RESPONSE
        clock.now = 2
        rest = [event async for event in stream]
        assert [kind for _, kind in rest] == [Event.ERROR]
        assert b"deadline" in rest[0][0] and REQUEST_ID.encode() in rest[0][0]
        await client.clean()
        assert not http.is_closed


@pytest.mark.asyncio
async def test_extension_preserves_interface_and_owned_client_cleanup():
    from flowspeech_tts_python.extension import FlowSpeechTTSExtension

    extension = FlowSpeechTTSExtension("tts")
    settings = await extension.create_config(
        json.dumps(
            {
                "params": {"api_key": "test-key", "voice_name": "Kore"},
            }
        )
    )
    assert extension.vendor() == "flowspeech"
    assert extension.synthesize_audio_sample_rate() == 24000
    client = await extension.create_client(settings, FakeTenEnv())
    await client.clean()
    assert client._client.is_closed


@pytest.mark.asyncio
async def test_invalid_wav_yields_only_error_not_header_bytes():
    responses = iter(
        [httpx.Response(200, json=job()), httpx.Response(200, content=b"RIFF-invalid")]
    )
    events, _, _ = await collect(lambda request: next(responses))
    assert len(events) == 1 and events[0][1] == Event.ERROR


@pytest.mark.asyncio
async def test_deadline_checked_after_response():
    clock = Clock()

    def respond(request):
        clock.now = 20
        return httpx.Response(200, json=job())

    events, _, _ = await collect(
        respond, settings=config(total_timeout=10), clock=clock
    )
    assert events[0][1] == Event.ERROR and b"deadline" in events[0][0]


@pytest.mark.asyncio
async def test_request_json_size_limit_precedes_network():
    def unexpected(request):
        pytest.fail("oversized JSON must not call the API")

    events, _, _ = await collect(unexpected, text="x" * (1024 * 1024))
    assert events[0][1] == Event.ERROR and b"1 MiB" in events[0][0]


@pytest.mark.asyncio
async def test_missing_content_length_still_bounded():
    events, _, _ = await collect(
        lambda request: httpx.Response(
            200,
            content=b" " * (1024 * 1024 + 1),
            headers={"content-length": "0"},
        )
    )
    assert events[0][1] == Event.ERROR and b"size limit" in events[0][0]


@pytest.mark.asyncio
async def test_parallel_get_is_rejected_without_resetting_active_call():
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.sleep(100)
        return httpx.Response(200, json=job())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = FlowSpeechTTSClient(config(), FakeTenEnv(), http_client=transport)
        generator = client.get("hello", "one")
        task = asyncio.create_task(anext(generator))
        await started.wait()
        previous_id = client.get_extra_metadata()["api_request_id"]
        events = [event async for event in client.get("other", "two")]
        assert events[0][1] == Event.ERROR
        assert client.get_extra_metadata()["api_request_id"] == previous_id
        await client.cancel()
        assert (await task)[1] == Event.FLUSH
        await generator.aclose()
        await client.clean()
