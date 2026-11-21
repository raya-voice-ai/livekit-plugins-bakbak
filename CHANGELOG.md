# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/); entries are generated from
[Conventional Commits](https://www.conventionalcommits.org/) by release-please.

## 1.0.0rc1 (2026-11-21)

The first release as `livekit-plugins-bakbak`, replacing `livekit-plugins-raya` 0.1.
See [Migrating from livekit-plugins-raya](README.md#migrating-from-livekit-plugins-raya).

### Features

- `bakbak.TTS` for LiveKit Agents 1.8.7 and later, with `synthesize()` and `stream()`.
- WebSocket transport (the default): one socket per `TTS`, opened by `prewarm()`, with
  every sentence sent on it the moment it is complete and queued by the server.
  Interruptions cancel on the server and keep the socket. Reconnects after idle closes,
  restarts (1001), draining and dropped connections resend the sentences that had no
  audio yet. A socket that stopped hearing the server's pings is replaced before use.
- HTTP transport (`transport="http"`): one Server-Sent Events request per sentence,
  with the next one started while the current one plays and audio delivered in order.
- `bakbak.SentenceTokenizer`, the Bakbak sentence segmenter: Indic-aware boundaries, no
  false splits in numbers, URLs, initials or titles. Streaming output is identical to
  batch output.
- 16-bit PCM straight from the server at 8, 16, 22.05 or 24 kHz; no numpy.
- Errors as LiveKit's `APIStatusError`, `APITimeoutError` and `APIConnectionError`, with
  the API's message, `error_code` (in `body`), `request_id` and `retryable`. Errors after
  audio are never retried, so audio is never played twice.
- `list_voices(model=, language=)`, `update_options()`, `extra_body` for new API fields,
  and BCP-47 language tags (`hi-IN` becomes `hi`).
- Metrics with time to first byte, connection reuse and the server's request ids.
- `BAKBAK_API_KEY` and `BAKBAK_BASE_URL`. `RAYA_API_KEY` and `RAYA_BASE_URL` still
  work, with a deprecation warning.
