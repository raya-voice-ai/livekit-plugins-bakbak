# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/); entries are generated from
[Conventional Commits](https://www.conventionalcommits.org/) by release-please.

## [1.0.0-rc.3](https://github.com/raya-voice-ai/livekit-plugins-bakbak/compare/v1.0.0-rc.2...v1.0.0-rc.3) (2026-11-22)


### Documentation

* point documentation links at docs.bakbak.si ([#6](https://github.com/raya-voice-ai/livekit-plugins-bakbak/issues/6)) ([2c7deb1](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/2c7deb16a29e7fcdbec967b0f569d4b3814a2de0))

## [1.0.0-rc.2](https://github.com/raya-voice-ai/livekit-plugins-bakbak/compare/v1.0.0-rc.1...v1.0.0-rc.2) (2026-11-22)


### Features

* add bakbak.TTS for LiveKit Agents ([98ddc51](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/98ddc51d53a3fa9e3a8224ba955b3916517f2d37))
* add the Bakbak sentence segmenter as a LiveKit SentenceTokenizer ([9ce5e37](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/9ce5e37149dc33b89721ee1301a7ca70a6261ce8))
* add the multiplexed WebSocket transport ([358fc8f](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/358fc8fc4b2b9af99b7318825bfdb44b406f153d))
* add the Server-Sent Events transport with in-order look-ahead ([2b5f416](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/2b5f4166d6ec611661b95a9fc17e5149ae6ff7ad))
* map API errors onto LiveKit's error classes ([0659370](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/0659370b0bd0382f6c84becd6549a3e8c72f9f9d))


### Bug Fixes

* always cancel on the server when an attempt ends ([bf817c0](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/bf817c009e382ec27f6fb36e6ce353b75e555a82))
* fail only its own request on malformed audio ([71bad96](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/71bad967ca626b2a958d2f4db5bb03426c2bb2da))
* fail waiting requests when a reconnect fails with any error ([6c55f7b](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/6c55f7bb0998af394aa842a2c94680e21f7c82c9))
* map every language's English name, including assamese and bangla ([3b7fd84](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/3b7fd8488ea3b340ec641c7634cf44eaa1f96c23))
* never wait more than a second to close a socket ([e68e27c](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/e68e27c7623482234467371cc9b340ba3d24ef38))
* release contexts that have nothing unfinished ([04d078e](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/04d078ea8998063b0d561f746bd5be5ef48c018c))
* stop repeating a validation error's text in the 422 message ([bcb6043](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/bcb6043c945603a580b8bc78cfb56c38f7584623))
* treat only the reconnect phrase as a draining refusal ([21a0796](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/21a0796d88369090a036187f49c48443d23d5d25))


### Performance Improvements

* make sentence segmentation linear ([2fcd064](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/2fcd0648ec53163cd3ac0e2b0f614205c3afce18))
* port the reference's bounded streaming look-behind ([9c6ea7f](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/9c6ea7fc78ed6da4b5e0a1c2bd390aad88947908))


### Documentation

* add the WAV and voice agent examples ([ba7dc2f](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/ba7dc2fb9614954178d3f48d768d3715391e8357))
* fix the README's PyPI links, the speed and m1 example, and list languages by model ([bcb6043](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/bcb6043c945603a580b8bc78cfb56c38f7584623))
* link the canonical docs and describe the hardened behaviour ([1b3e692](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/1b3e6929edf89f0d77d50e54483766108a33c7ca))
* write the README, changelog, contributing and security guides ([3020400](https://github.com/raya-voice-ai/livekit-plugins-bakbak/commit/302040023fb07e21e67b5731c9c335e7366be5b2))

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
- Sentence segmentation is linear in the text's length, streamed or whole.

### Security

- Redirects are never followed, on HTTP or the WebSocket handshake, so the key cannot
  be sent to another origin; a 3xx is raised with its status.
- The key is not reachable from errors (including chained causes) or reprs, and a
  key with whitespace or control characters is refused without being echoed.
