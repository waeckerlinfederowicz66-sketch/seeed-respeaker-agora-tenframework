# flowspeech_tts_python

TEN Framework text-to-speech extension for [FlowSpeech](https://flowspeech.io/).
Version 0.2.0 uses public API v1 jobs: submit text, poll for completion, download
WAV, then emit 24 kHz mono 16-bit PCM frames to TEN. It does not stream generation;
first audio is available only after the complete recording has been downloaded.

This change is prepared for the existing [provider PR #1](https://github.com/Seeed-Projects/seeed-respeaker-agora-tenframework/pull/1).
Do not assume it is merged, installed by the default example, or released.

## Configuration

Create a developer API key in FlowSpeech API settings. Generation requires an
active paid Basic, Pro or Scale subscription and enough available credits.
Browser cookies and plugin login tokens do not authenticate the public API.

Set `FLOWSPEECH_API_KEY` on the server. Do not commit the key to source control.

```json
{
  "total_timeout": 600.0,
  "max_retries": 2,
  "params": {
    "api_key": "${env:FLOWSPEECH_API_KEY}",
    "base_url": "https://flowspeech.io",
    "voice_name": "Kore"
  }
}
```

The timeout is a finite positive total budget in seconds for submission, retries,
polling, download, conversion and PCM emission, at most 7200 seconds. Expiry after
conversion or while the consumer pauses emission stops further audio and END.
`max_retries` is zero to ten retries per
HTTP operation. `base_url` accepts an HTTPS origin or the complete
`/api/v1/tts/jobs` endpoint, without query, fragment or embedded credentials.
Only configure a trusted host because it receives the API key and text.

Only `api_key`, `base_url` and `voice_name` belong in `params`. Unsupported model,
source, speed, format, sample-rate, text, legacy speaker and segment fields are
rejected. This TEN interface synthesizes one text segment with one voice per
`get(text, request_id)`; multi-speaker jobs are not exposed by this adapter.

## Audio and Recovery

Requests use Bearer authentication and a fresh API UUID for each invocation.
TEN's `request_id` remains a correlation ID: it may be non-UUID and may identify
multiple text segments. Internal submission retries preserve the API UUID and
the exact JSON body. Download retries reuse the result URL and never regenerate.

`get_extra_metadata()` exposes `voice`, `api_request_id` and `job_id` for the
current or most recent invocation. Errors include the recovery IDs, recognized
public API error codes and canonical UUID trace IDs when available. Raw upstream
messages, unrecognized codes and malformed trace IDs are not logged or emitted.
Invalid configuration reports a fixed error without rejected credential values.
Keep recovery IDs when a call fails or is stopped.
Starting a new `get()` is a new recording, not recovery of an earlier one.

Polling waits at least five seconds and respects a longer numeric or HTTP-date
`Retry-After`. Response-body and cleanup transport failures also retain the known
HTTP status and cooldown; 401/403 failures are not retried.
Pending states are queued, running, merging and settling. Failed,
cancelled, deleted or malformed jobs produce an error event instead of audio.
HTTP 200 alone is not success. API 401/403 responses produce INVALID_KEY_ERROR;
a storage download 403 is an ordinary ERROR.

Every job response must include a canonical UUID `requestId` matching this
invocation's API UUID and nonnegative integer usage counters. IDs are normalized
before comparison. Missing or mismatched identity, and malformed usage, fail
before audio download or PCM emission.

Downloads contain no API credentials, cookies, default client auth or custom
API headers. Redirects are rejected. JSON bodies are limited to 1 MiB and audio
to 100 MiB. The WAV container is parsed with Python's standard `wave` module,
including optional metadata chunks. Its encoding and data length are verified
before any PCM is emitted. WAV headers are never sent to TEN as PCM samples.
Each RESPONSE contains at most 4096 bytes, aligned to two-byte samples, followed
by END.

TEN cancellation interrupts local requests or waits and emits FLUSH. It does
not call the server cancellation endpoint, so accepted jobs may continue and
consume credits. External asyncio task cancellation propagates normally.
Cleanup cancels active local work and closes only the client-owned HTTP session.

For recovery, use `GET /api/v1/tts/jobs?requestId=UUID` with the API UUID. To
cancel server generation, use `POST /api/v1/tts/jobs/{id}/cancel`. Account checks
use `GET /api/v1/quota` (top-level quota, subscription, limits) and
`GET /api/v1/voices` (top-level voices). These are direct API operations, not
methods added to the TEN adapter. See the
[public API guide](https://flowspeech.io/docs/public-api). Never send Bearer
authentication when downloading `result.url`.

## Local Tests

With HTTPX, Pydantic, pytest and pytest-asyncio installed, run from the repository
root without an API key:

```bash
FLOWSPEECH_STANDALONE_TESTS=1 \
PYTHONPATH=ai_agents/agents/ten_packages/extension \
python -m pytest ai_agents/agents/ten_packages/extension/flowspeech_tts_python/tests -q
```

The explicit standalone flag installs test-only TEN interface shims. HTTP uses
MockTransport; no generation or credits are consumed. This verifies protocol,
audio conversion and event behavior, not native TEN binaries, reSpeaker hardware
or live account access. In an installed TEN environment, omit that flag to use
the real interfaces.

## Migration

Upgrade to the v1-capable extension before using these settings. Legacy
`/api/ai/text-to-speech` and base64 responses are no longer supported. Remove
obsolete params, rotate legacy keys when required, and allow enough timeout
for job processing. Review [CHANGELOG.md](CHANGELOG.md) before deploying.
