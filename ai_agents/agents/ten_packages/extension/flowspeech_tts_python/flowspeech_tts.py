import asyncio
import io
import json
import math
import struct
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from ten_ai_base.const import LOG_CATEGORY_VENDOR
from ten_ai_base.struct import TTS2HttpResponseEventType
from ten_ai_base.tts2_http import AsyncTTS2HttpClient
from ten_runtime import AsyncTenEnv

from .config import FlowSpeechTTSConfig, JOBS_PATH, secure_url

BYTES_PER_SAMPLE = 2
DEFAULT_CHUNK_SIZE = 4096
MAX_JSON_BYTES = 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024
PUBLIC_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_api_key",
        "insufficient_scope",
        "insufficient_credits",
        "subscription_required",
        "rate_limited",
        "service_unavailable",
        "job_not_found",
        "job_already_completed",
        "idempotency_conflict",
    }
)


class Result(BaseModel):
    model_config = ConfigDict(strict=True)
    url: str
    mimeType: Literal["audio/wav"]
    sampleRate: Literal[24000]
    channels: Literal[1]
    bitsPerSample: Literal[16]


class ErrorDetail(BaseModel):
    model_config = ConfigDict(strict=True)
    code: str
    message: str
    retryable: bool = False


class Usage(BaseModel):
    model_config = ConfigDict(strict=True)
    billableCharacters: int = Field(ge=0)
    reservedCredits: int = Field(ge=0)
    chargedCredits: int = Field(ge=0)


class Job(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    requestId: str
    usage: Usage
    status: Literal[
        "queued", "running", "merging", "settling", "success", "failed", "cancelled"
    ]
    result: Result | None = None
    resultDeleted: bool = False
    error: ErrorDetail | None = None

    @field_validator("id", "requestId")
    @classmethod
    def canonical_identifier(cls, value: str) -> str:
        normalized = str(UUID(value))
        if normalized != value.lower():
            raise ValueError("Expected a canonical hyphenated UUID")
        return normalized


class ErrorResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    error: ErrorDetail
    traceId: str | None = None


class FlowSpeechError(RuntimeError):
    def __init__(self, message: str, status: int = 502, api: bool = True):
        super().__init__(message)
        self.status = status
        self.api = api


class ResponseReadError(httpx.TransportError):
    def __init__(self, response: httpx.Response):
        super().__init__("FlowSpeech response body or cleanup failed")
        self.status = response.status_code
        self.retry_after = response.headers.get("retry-after")


def validate_wav_structure(content: bytes) -> None:
    if (
        len(content) < 12
        or content[:4] != b"RIFF"
        or content[8:12] != b"WAVE"
        or int.from_bytes(content[4:8], "little") != len(content) - 8
    ):
        raise ValueError("Invalid RIFF container length")
    stream = io.BytesIO(content)
    stream.seek(12)
    for header in iter(lambda: stream.read(8), b""):
        if len(header) != 8:
            raise ValueError("Truncated WAV chunk header")
        size = int.from_bytes(header[4:], "little")
        end = stream.tell() + size + size % 2
        if end > len(content):
            raise ValueError("Truncated WAV chunk")
        if header[:4] == b"fmt " and stream.read(min(size, 16))[2:] != struct.pack(
            "<HIIHH", 1, 24000, 48000, 2, 16
        ):
            raise ValueError("Invalid PCM format fields")
        if header[:4] == b"data" and size % 2:
            raise ValueError("Unaligned PCM data")
        stream.seek(end)


def wav_to_pcm(audio: bytes) -> bytes:
    try:
        validate_wav_structure(audio)
        with wave.open(io.BytesIO(audio), "rb") as reader:
            if (
                reader.getframerate(),
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getcomptype(),
            ) != (24000, 1, 2, "NONE"):
                raise ValueError("Unsupported WAV encoding")
            pcm = reader.readframes(reader.getnframes() + 1)
            if not pcm or len(pcm) != reader.getnframes() * BYTES_PER_SAMPLE:
                raise ValueError("Empty or truncated WAV")
            return pcm
    except (wave.Error, EOFError, ValueError) as exc:
        raise FlowSpeechError(
            "FlowSpeech requires complete 24 kHz mono 16-bit WAV audio"
        ) from exc


class FlowSpeechTTSClient(AsyncTTS2HttpClient):
    def __init__(
        self,
        config: FlowSpeechTTSConfig,
        ten_env: AsyncTenEnv,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self.config = config
        self.params = config.resolved_params()
        self.ten_env = ten_env
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False
        )
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._cancelled = False
        self._active = False
        self._task: asyncio.Task[bytes] | None = None
        self._api_request_id = ""
        self._job_id = ""
        self._deadline = 0.0
        self._url = str(secure_url(self.params.base_url).copy_with(path=JOBS_PATH))

    async def cancel(self) -> None:
        self._cancelled = True
        if self._task and not self._task.done():
            self._task.cancel()

    def _remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise FlowSpeechError("FlowSpeech total deadline exceeded", 408)
        return remaining

    def _retry_after(self, value: str | None) -> float:
        if not value:
            return 5.0
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                seconds = (
                    date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
                    - self._wall_clock()
                )
            except (ValueError, TypeError, OverflowError):
                return 5.0
        return max(5.0, seconds) if math.isfinite(seconds) else 5.0

    async def _wait(
        self, response: httpx.Response | None = None, *, retry_after: str | None = None
    ) -> None:
        value = (
            retry_after
            if retry_after is not None
            else (response.headers.get("retry-after") if response is not None else None)
        )
        delay = self._retry_after(value)
        if delay >= self._remaining():
            raise FlowSpeechError(
                "FlowSpeech deadline cannot accommodate Retry-After/poll delay", 408
            )
        await self._sleep(delay)
        self._remaining()

    def _error_code(self, code: str) -> str:
        return (
            code
            if code in PUBLIC_ERROR_CODES and self.params.api_key not in code
            else "upstream_error"
        )

    def _trace_id(self, value: str | None) -> str | None:
        try:
            normalized = str(UUID(value)) if value else None
        except ValueError:
            return None
        if (
            normalized
            and normalized == value.lower()
            and self.params.api_key not in normalized
        ):
            return normalized
        return None

    async def _read(self, request: httpx.Request, download: bool) -> httpx.Response:
        response = await self._client.send(
            request, stream=True, auth=None, follow_redirects=False
        )
        try:
            maximum = MAX_AUDIO_BYTES if download else MAX_JSON_BYTES
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise FlowSpeechError("Encoded FlowSpeech response rejected")
            try:
                length = int(response.headers.get("content-length", "0"))
            except ValueError as exc:
                raise FlowSpeechError("Invalid FlowSpeech Content-Length") from exc
            if not 0 <= length <= maximum:
                raise FlowSpeechError("FlowSpeech response exceeds size limit")
            with io.BytesIO() as content:
                async for chunk in response.aiter_bytes():
                    self._remaining()
                    if content.tell() + len(chunk) > maximum:
                        raise FlowSpeechError("FlowSpeech response exceeds size limit")
                    content.write(chunk)
                self._remaining()
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=content.getvalue(),
                    request=request,
                )
        except httpx.TransportError:
            raise ResponseReadError(response) from None
        finally:
            try:
                await response.aclose()
            except httpx.TransportError:
                raise ResponseReadError(response) from None

    async def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        *,
        download: bool = False,
    ) -> httpx.Response:
        for attempt in range(self.config.max_retries + 1):
            remaining = self._remaining()
            request = httpx.Request(
                method,
                url,
                content=body,
                headers={"Accept-Encoding": "identity"}
                if download
                else {
                    "Authorization": f"Bearer {self.params.api_key}",
                    "Idempotency-Key": self._api_request_id,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
                extensions={
                    "timeout": {
                        phase: remaining
                        for phase in ("connect", "read", "write", "pool")
                    }
                },
            )
            try:
                response = await self._read(request, download)
            except httpx.TransportError as exc:
                status = exc.status if isinstance(exc, ResponseReadError) else None
                retryable = (
                    status is None
                    or status == 429
                    or (200 <= status < 300 or 500 <= status < 600)
                )
                if not retryable or attempt == self.config.max_retries:
                    raise FlowSpeechError(
                        "FlowSpeech download transport failed"
                        if download
                        else "FlowSpeech API transport failed",
                        status or 502,
                        not download,
                    ) from exc
                await self._wait(
                    retry_after=exc.retry_after
                    if isinstance(exc, ResponseReadError)
                    else None
                )
                continue
            if response.status_code in (
                (200,) if download or method == "GET" else (200, 202)
            ):
                return response
            if response.is_redirect:
                raise FlowSpeechError("FlowSpeech unexpected redirect rejected")
            try:
                error = ErrorResponse.model_validate_json(response.content)
            except ValueError:
                error = None
            retryable = response.status_code == 429 or (
                response.status_code >= 500
                and (download or (error is not None and error.error.retryable))
            )
            if retryable and attempt < self.config.max_retries:
                await self._wait(response)
                continue
            detail = f"HTTP {response.status_code}"
            if error is not None:
                detail += f"; code={self._error_code(error.error.code)}"
                trace_id = self._trace_id(error.traceId)
                if trace_id:
                    detail += f"; traceId={trace_id}"
            raise FlowSpeechError(
                f"FlowSpeech {'download' if download else 'API'} error: {detail}",
                response.status_code,
                not download,
            )
        raise FlowSpeechError("FlowSpeech retries exhausted")

    async def _generate(self, text: str) -> bytes:
        body = json.dumps(
            {
                "input": {
                    "mode": "single",
                    "text": text,
                    "voiceName": self.params.voice_name,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > MAX_JSON_BYTES:
            raise FlowSpeechError("FlowSpeech request exceeds 1 MiB", 413)
        response = await self._request("POST", self._url, body)
        while True:
            try:
                job = Job.model_validate_json(response.content)
            except ValueError as exc:
                raise FlowSpeechError(
                    "FlowSpeech returned an invalid job, ID, status or result"
                ) from exc
            if job.requestId != self._api_request_id:
                raise FlowSpeechError("FlowSpeech returned a different request ID")
            if self._job_id and str(job.id) != self._job_id:
                raise FlowSpeechError("FlowSpeech returned a different job ID")
            self._job_id = str(job.id)
            if job.status in ("failed", "cancelled"):
                detail = self._error_code(job.error.code) if job.error else job.status
                raise FlowSpeechError(f"FlowSpeech job {job.status}: {detail}", 422)
            if job.status == "success":
                if job.resultDeleted:
                    raise FlowSpeechError(
                        "FlowSpeech recording was deleted (resultDeleted=true)", 410
                    )
                if job.result is None:
                    raise FlowSpeechError("FlowSpeech successful job has no result")
                try:
                    url = str(secure_url(job.result.url))
                except (ValueError, httpx.InvalidURL) as exc:
                    raise FlowSpeechError(
                        "FlowSpeech returned an unsafe audio URL"
                    ) from exc
                audio = await self._request("GET", url, download=True)
                pcm = wav_to_pcm(audio.content)
                self._remaining()
                return pcm
            await self._wait(response)
            response = await self._request("GET", f"{self._url}/{self._job_id}")

    async def get(
        self, text: str, request_id: str
    ) -> AsyncIterator[tuple[bytes | None, TTS2HttpResponseEventType]]:
        if self._active:
            yield (
                b"FlowSpeech client already has an active request",
                TTS2HttpResponseEventType.ERROR,
            )
            return
        if not text.strip():
            yield None, TTS2HttpResponseEventType.END
            return
        self._active = True
        self._cancelled = False
        self._api_request_id = str(uuid4())
        self._job_id = ""
        self._deadline = self._clock() + self.config.total_timeout
        self._task = asyncio.create_task(self._generate(text))
        try:
            pcm = await asyncio.wait_for(self._task, timeout=self.config.total_timeout)
            for offset in range(0, len(pcm), DEFAULT_CHUNK_SIZE):
                if self._cancelled:
                    yield None, TTS2HttpResponseEventType.FLUSH
                    return
                self._remaining()
                yield (
                    pcm[offset : offset + DEFAULT_CHUNK_SIZE],
                    TTS2HttpResponseEventType.RESPONSE,
                )
            self._remaining()
            yield None, TTS2HttpResponseEventType.END
        except asyncio.CancelledError:
            if not self._cancelled:
                raise
            yield None, TTS2HttpResponseEventType.FLUSH
        except (FlowSpeechError, asyncio.TimeoutError) as exc:
            detail = str(exc) or "FlowSpeech total deadline exceeded"
            message = f"{detail} (api_request_id={self._api_request_id}, job_id={self._job_id or 'unknown'})"
            self.ten_env.log_error(
                f"vendor_error: {message}; TEN request_id: {request_id}",
                category=LOG_CATEGORY_VENDOR,
            )
            kind = (
                TTS2HttpResponseEventType.INVALID_KEY_ERROR
                if (
                    isinstance(exc, FlowSpeechError)
                    and exc.api
                    and exc.status in (401, 403)
                )
                else TTS2HttpResponseEventType.ERROR
            )
            yield message.encode(), kind
        finally:
            self._active = False
            self._task = None

    async def clean(self) -> None:
        await self.cancel()
        if self._task and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)
        if self._owns_client:
            await self._client.aclose()

    def get_extra_metadata(self) -> dict[str, Any]:
        return {
            "voice": self.params.voice_name,
            "api_request_id": self._api_request_id,
            "job_id": self._job_id,
        }
