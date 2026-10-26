# Changelog

## 0.2.0 (Unreleased)

- Replace the retired synchronous endpoint and base64 envelope with public API v1 submit, poll and download
- Require a developer API key, an active paid API subscription and available credits
- Parse the downloaded WAV with the standard library and emit only 24 kHz mono 16-bit PCM frames, including WAV files with additional metadata chunks
- Require and normalize job/request UUIDs, match every returned request ID to the invocation, and validate usage counters before downloading audio
- Add stable per-submission idempotency UUIDs, bounded retries, Retry-After handling, total timeout, result validation and recovery metadata
- Reject obsolete or unsupported params instead of silently forwarding or dropping them
- Stop local network/polling work on TEN cancellation and emit FLUSH; cancellation does not automatically cancel the server job

This version is prepared for the existing provider PR, not released or accepted upstream
