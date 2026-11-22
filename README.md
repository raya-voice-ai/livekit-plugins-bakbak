# livekit-plugins-bakbak

Bakbak text-to-speech for [LiveKit Agents](https://docs.livekit.io/agents/): natural,
low-latency speech for Indian languages, from [Raya](https://getraya.app).

```python
from livekit.plugins import bakbak

session = AgentSession(stt=..., llm=..., tts=bakbak.TTS(language="hi"))
```

- **Fast.** One WebSocket per agent, opened before the first reply. Each sentence of
  the LLM's reply is sent the moment it is complete and the server queues them, so
  the first audio arrives in about 22 ms (measured, below) and there is no gap between
  sentences.
- **Built for Indian text.** The Bakbak sentence segmenter knows the danda (`।`, `॥`),
  never splits inside numbers, URLs, initials or titles like "Dr." and "डॉ.", and keeps
  Indic conjuncts whole.
- **Safe under failure.** Interruptions cancel the server's work and keep the
  connection. Restarts and dropped connections are handled without losing a sentence.
  Audio is never replayed: once a sentence has been heard, a failure is reported, not
  retried.

Hindi, Marathi, Telugu, Kannada, Bengali, Assamese, Gujarati, Nepali, Malayalam,
Tamil, Punjabi and English (Indian and US), code-mixed text included.

## Install

```bash
pip install livekit-plugins-bakbak
```

Python 3.10 to 3.14, `livekit-agents` 1.8.7 or later (1.x).

## Quickstart

Set your API key:

```bash
export BAKBAK_API_KEY="..."
```

Use it in an agent:

```python
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import bakbak, deepgram, openai

server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=bakbak.TTS(language="hi"),
    )
    await session.start(agent=Agent(instructions="हिंदी में छोटे जवाब दें।"), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
```

Or on its own, outside a room:

```python
import asyncio

import aiohttp
from livekit import rtc
from livekit.plugins import bakbak


async def main() -> None:
    async with aiohttp.ClientSession() as session:  # inside an agent, LiveKit provides one
        tts = bakbak.TTS(http_session=session)
        frames = [ev.frame async for ev in tts.synthesize("नमस्ते! आप कैसे हैं?")]
        await tts.aclose()
    with open("hello.wav", "wb") as f:
        f.write(rtc.combine_audio_frames(frames).to_wav_bytes())


asyncio.run(main())
```

[`examples/`](https://github.com/raya-voice-ai/livekit-plugins-bakbak/tree/main/examples) has a runnable
[WAV script](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/examples/synthesize_to_wav.py)
and a full [voice agent](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/examples/agent.py)
(`pip install "livekit-plugins-bakbak[examples]"`).

## Configuration

`bakbak.TTS(...)` takes keyword arguments only.

| Option | Default | |
|---|---|---|
| `api_key` | `BAKBAK_API_KEY` | Your API key. |
| `voice_id` | `009ae8b5-4a03-492d-af1c-3055989ac734` (Anika M1, Hindi) | A voice belongs to one model; see [Voices](#voices). |
| `language` | `"hi"` | The language of the text. Codes, BCP-47 tags and names all work: see [Languages](#languages). |
| `model` | `"m1"` | `"m1"` or `"standard"`; must match the voice's model. |
| `speed` | not sent (the server uses 1.0) | Speaking rate, 0.5 to 1.5. Changes standard voices only: m1 voices accept it and keep their own pace. |
| `sample_rate` | `24000` | `8000`, `16000`, `22050` or `24000` Hz. Audio is 16-bit mono PCM at this rate. |
| `base_url` | `BAKBAK_BASE_URL`, else `https://hub.getraya.app` | The API's address. |
| `transport` | `"websocket"` | `"websocket"` or `"http"`; see [Transports](#transports-and-latency). |
| `tokenizer` | `bakbak.SentenceTokenizer()` | How text is split into requests; see [Sentences](#sentences). Any LiveKit `SentenceTokenizer` works. |
| `text_pacing` | `False` | `True` (or a `tts.SentenceStreamPacer`) batches sentences by how much audio is still queued. |
| `http_session` | LiveKit's shared session | An `aiohttp.ClientSession` of your own. |
| `extra_body` | none | Extra request fields, for API features this plugin does not model yet. They are added to every request and cannot replace the fields above. |

Environment variables are read when the `TTS` is created, never at import.

Change the voice settings at any time; they apply from the next utterance. Here, a
standard Marathi voice, a little faster (`speed` has no effect on m1 voices):

```python
voice = (await tts.list_voices(model="standard", language="mr"))[0]
tts.update_options(voice_id=voice.id, language="mr", model="standard", speed=1.1)
```

`tts.model` and `tts.provider` (`"Bakbak"`) label LiveKit's metrics.

## Voices

```python
for voice in await tts.list_voices(model="m1", language="hi"):
    print(voice.id, voice.name, voice.language, voice.model)
```

`list_voices()` returns every voice. `model` and `language` filter the list on your side.
A voice works only with its own model: an m1 voice with `model="standard"` is refused.

## Languages

Known codes: `hi` `mr` `te` `kn` `bn` `as` `gu` `ne` `ml` `ta` `pa` `en` `en-in` `en-us`.
Other strings are passed through, so a language added on the server works without a new
release.

The plugin accepts what LiveKit users tend to have at hand and sends the API's code:

| You pass | Sent |
|---|---|
| `"hi-IN"`, `"hi_IN"`, `"Hindi"` | `hi` |
| `"marathi"`, `"mr-IN"` | `mr` |
| `"bengali"`, `"bangla"`, `"bn-IN"` | `bn` |
| `"assamese"`, `"punjabi"`, `"panjabi"` | `as`, `pa`, `pa` |
| `"en-IN"` / `"en-US"` | `en-in` / `en-us` (the standard model's English voices) |
| `"en"`, `"en-GB"` | `en` (the m1 English voices) |

The English name of every language above works too (`"telugu"`, `"kannada"`,
`"gujarati"`, `"nepali"`, `"malayalam"`, `"tamil"`, `"english"`, any case). The m1
English voices use `en`, not `en-in`.

## Transports and latency

### `transport="websocket"` (default)

One WebSocket per `TTS` instance carries every utterance (the protocol is in the
[WebSocket API reference](https://docs.litwizlabs.com/api-reference/tts-websocket)):

- `prewarm()` opens it. `AgentSession` calls this when the agent starts, so the first
  reply pays no handshake. Without it, the first utterance opens the socket.
- Each utterance is a *context* on the socket. Each sentence is sent the moment the
  segmenter knows it is complete, while the LLM is still writing. The server runs a
  context's sentences one after another without a round trip between them, so audio
  flows continuously.
- Interrupting the agent cancels the context on the server, drops its late audio, and
  keeps the socket for the next turn. The cancel is sent even when the interruption
  lands while an error is being handled.
- The client always reads, so the server's pings are answered. A socket that has heard
  nothing for 45 s (the server pings every 30 s) is replaced before it is used, rather
  than discovered dead by a request.
- The server closes an idle socket after 5 minutes; the next utterance reconnects. When
  the server restarts (close 1001), or refuses a sentence because it is draining (a 503
  whose message says "Reconnect and send the request again"), the sentences it has not
  started are sent again on a new connection, and the draining socket is closed once
  nothing uses it. Any other 503, such as `service_unavailable` when balances cannot be
  checked, is an ordinary retryable error that LiveKit retries.
- If the connection drops, sentences with no audio yet are sent again; the first
  reconnect is immediate, and at most two more follow with backoff. Closing a socket
  never waits more than a second for a peer that stopped answering.
- A chunk of audio that does not decode fails only its own sentence (retryable if
  nothing was heard yet); the connection stays.

### `transport="http"`

One Server-Sent Events request per sentence. The next sentence's request starts as soon
as the current one is producing audio, and the audio is delivered strictly in order.
This look-ahead means an utterance can use **two** of your account's concurrent
requests. If the second is refused (for example by a concurrency limit), it is simply
sent again when its turn comes. Use HTTP where WebSockets are blocked. Otherwise prefer
the default.

### Measured

Time from `push_text()` to the first audio frame, through the full public path, with a
warm connection. Measured 2026-11-21 against a staging deployment, from a client close
to it, model m1, N = 15 each:

| Transport | p50 | p90 |
|---|---|---|
| WebSocket | 22.2 ms | 24.4 ms |
| HTTP | 30.7 ms | 32.3 ms |

Your network distance to the API adds to both. The WebSocket also saves the per-request
gateway work the HTTP path repeats.

### Latency tips

- Keep the default WebSocket transport, and let `prewarm()` run (AgentSession does it).
- Ask for the rate you will play: `sample_rate=8000` or `16000` for telephony avoids a
  resample.
- The first sentence is sent as soon as it ends, however short (`first_min_chars=1`).
  If your replies open with a long sentence, `SentenceTokenizer(eager_first_clause=True)`
  also sends the first clause on its own.
- `text_pacing` sends more text per request when plenty of audio is queued. That saves
  work on interruptions, at no cost to the first audio.

## Sentences

`bakbak.SentenceTokenizer` is the Bakbak segmenter, the same one every official Bakbak
client uses. It splits on `.` `!` `?` `…` `।` `॥` (and full-width marks) followed by a
space or a newline. It does not split:

- inside numbers, URLs and e-mail addresses (`3.14`, `getraya.app`);
- after initials and titles (`A.`, `e.g.`, `Dr.`, `डॉ.`);
- after list markers at the start of a line (`1.`);
- after abbreviations such as `etc.` or `no.` unless a capital letter follows.

Short sentences are merged so that every request has something to say. Text with
nothing speakable (only punctuation or an emoji) is not sent. Streaming gives exactly
the sentences that splitting the whole text gives, and each one is released as soon as
its end is certain.

```python
tts = bakbak.TTS(
    tokenizer=bakbak.SentenceTokenizer(
        min_chars=20,  # shorter sentences are merged with the next one
        first_min_chars=1,  # the first sentence goes out as soon as it ends
        max_chars=400,  # longer ones are wrapped at a comma or a space (max 5000)
        eager_first_clause=False,  # also end the first sentence at a clause (, ; : —)
        first_clause_min_chars=24,  # ... if that clause is at least this long
    )
)
```

`bakbak.IndicSentenceTokenizer(min_sentence_len=..., max_sentence_len=...)` still works,
with a deprecation warning.

## Errors and retries

Errors are LiveKit's own classes, so `FallbackAdapter` and your error handling work
unchanged:

| What happened | Raised |
|---|---|
| The API refused the request (bad voice, no credits, rate limit, server error) | `APIStatusError`: `status_code`, `request_id`, `retryable`, and `body` (the parsed JSON, with `error_code` and fields such as `required_inr`) |
| No answer within `conn_options.timeout` | `APITimeoutError` |
| The API could not be reached, or a stream ended before any audio | `APIConnectionError` |
| A stream broke off after its audio had started | `APIStatusError` with `status_code=-1`, `retryable=False`, and the request id |

The message is the API's own sentence, naming the field when a request is invalid
("The request is malformed or invalid. voice_id: Field required"). Every
`error_code` is described in the [errors reference](https://docs.litwizlabs.com/documentation/tts-errors).
A redirect is never followed (it would take your key to another address): a 3xx is
raised as an `APIStatusError` with that status.

LiveKit's base classes retry `retryable` errors according to `conn_options`
(`max_retry`, `retry_interval`), each time on a fresh context. A retry happens only
before any audio of the utterance has been delivered: a request is billed once its
audio has left the service, and a retry would play the start twice. Errors after audio
are therefore never retryable. That includes `synthesize()`, which LiveKit would
otherwise retry. `conn_options.timeout` bounds both connecting and each wait for the
server's next message.

## Logging and privacy

The plugin logs to `livekit.plugins.bakbak`. Logs never contain your API key or the
text being spoken. Messages from the server are logged only under LiveKit's
`lk.pii.*` fields. The key is not reachable from any error the plugin raises (causes
that would carry request headers are dropped) and is not shown in any `repr`; a key
with whitespace or control characters is refused when the `TTS` is created, without
echoing it. The plugin sends a `User-Agent` and `X-Bakbak-Client` header, talks to
nothing but the API's base URL, and never follows a redirect.

## How this plugin differs from the shared client behaviour

The official Bakbak clients follow one behaviour specification. Inside LiveKit, a few
points follow LiveKit's conventions instead:

- **Retries** are LiveKit's: its retry count and its waits (`conn_options`), not the
  SDKs' exponential backoff or `Retry-After`.
- **Timeouts** come from `conn_options.timeout` (LiveKit's default is 10 s), not a
  separate 5 s connect / 60 s read pair.
- **A missing or malformed key** raises `ValueError` (naming `api_key` and
  `BAKBAK_API_KEY`), as other LiveKit plugins do.
- **`speed`** is sent only when you set it.
- **`RAYA_API_KEY` and `RAYA_BASE_URL`** are still read when the `BAKBAK_` variables are
  unset, with a one-time deprecation warning.
- **Text with nothing speakable** (such as `"..."`) produces 20 ms of silence rather than
  an error, because LiveKit expects audio for every utterance.
- **A refused WebSocket upgrade** reports its status, but not the gateway's request id,
  which `aiohttp` does not expose.

## Migrating from livekit-plugins-raya

`livekit-plugins-bakbak` replaces `livekit-plugins-raya` 0.1.

```diff
-pip install livekit-plugins-raya
+pip install livekit-plugins-bakbak
```

```diff
-from livekit.plugins import raya
-tts = raya.TTS(language="hi")
+from livekit.plugins import bakbak
+tts = bakbak.TTS(language="hi")
```

| | livekit-plugins-raya | livekit-plugins-bakbak |
|---|---|---|
| API key variable | `RAYA_API_KEY` | `BAKBAK_API_KEY` (the old name still works, with a warning) |
| Base URL variable | `RAYA_BASE_URL` | `BAKBAK_BASE_URL` (likewise) |
| Default voice | Anjura M1 (`3fe4afbc-…`) | Anika M1 (`009ae8b5-…`); pass `voice_id` to keep yours |
| Connection | one HTTP request per sentence, one after another | one WebSocket, every sentence queued on it (`transport="http"` for the old style, now overlapped) |
| Audio | 32-bit float converted with numpy | 16-bit PCM straight from the server; no numpy dependency |
| `encoding`, `codec` options | accepted | removed; the format is always 16-bit PCM at `sample_rate` |
| `speed` | always sent (1.0) | sent only when set |
| First sentence | waited for 20 characters | sent as soon as it ends (`first_min_chars=1`) |
| Tokenizer | `IndicSentenceTokenizer(min_sentence_len, max_sentence_len)` | `SentenceTokenizer(min_chars, max_chars, …)`; the old name still works |
| `list_voices()` | every voice | optional `model=` and `language=` filters |
| Errors | text bodies, every stream error retryable | parsed bodies with `error_code` and `request_id`; errors after audio never retried |
| Languages | codes only | BCP-47 tags and names too (`hi-IN`, `"hindi"`) |
| `provider` | `"Raya"` | `"Bakbak"` |

## Compatibility

- `livekit-agents` 1.8.7 and later 1.x releases are supported and tested on the oldest
  and the newest. The 2.0 betas are a rewrite of LiveKit's plugin interface and are not
  supported yet.
- Python 3.10 to 3.14.

## Links

- [This plugin's documentation](https://docs.litwizlabs.com/documentation/tts-livekit)
- [WebSocket API reference](https://docs.litwizlabs.com/api-reference/tts-websocket)
- [Errors](https://docs.litwizlabs.com/documentation/tts-errors)
- [LiveKit Agents documentation](https://docs.livekit.io/agents/)
- [Changelog](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/CHANGELOG.md) ·
  [Contributing](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/CONTRIBUTING.md) ·
  [Security](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/SECURITY.md)

## License

Apache-2.0. See [LICENSE](https://github.com/raya-voice-ai/livekit-plugins-bakbak/blob/main/LICENSE).
