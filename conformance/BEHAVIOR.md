# Bakbak client behaviour specification (version 1)

This document is the contract every official Bakbak text-to-speech client follows:
the Python SDK (`bakbak` on PyPI), the JavaScript SDK (`bakbak` on npm), the MCP
server (`bakbak-mcp`), and the LiveKit Agents plugin (`livekit-plugins-bakbak`).
It is copied byte for byte into each repository under `conformance/`, next to the
fixtures that test it. Change it in one place, sync it, and update every client.

Words in **must** are requirements that the conformance fixtures or each repo's
tests check. Everything else is guidance.

Scope: text-to-speech only. Speech-to-text and the Raya agents API are out of scope.

---

## 1. The API in one page

Base URL `https://hub.getraya.app`. WebSocket URL: the base URL with `https` → `wss`
(or `http` → `ws`) plus the path below.

| Operation | Request | Response |
|---|---|---|
| Synthesize a whole file | `POST /v1/text-to-speech`, JSON body | the audio bytes |
| Stream one request | `POST /v1/text-to-speech/stream`, JSON body | Server-Sent Events |
| Stream many requests | `GET /v1/text-to-speech/ws`, then JSON text messages | JSON text messages |
| List voices | `GET /v1/voices` | `{"voices":[{id,name,language,model}],"count":N}` |

Authentication: header `X-API-Key: <key>`. Browsers cannot set headers on a
WebSocket, so the WebSocket also accepts `?X-API-Key=<key>` in the URL.

### Request body (all three synthesis operations)

| Field | Type | Notes |
|---|---|---|
| `text` | string, required | 1–5,000 characters. Hindi must be Devanagari. |
| `voice_id` | string, required | From `/v1/voices`. A voice belongs to one model. |
| `language` | string, required | Known today: `hi mr te kn bn as gu ne ml ta pa en en-in en-us`. |
| `model` | string | `standard` (server default) or `m1`. Must match the voice's model. |
| `speed` | number | 0.5–1.5, default 1.0. |
| `output_format` | object | `{container, encoding, sample_rate}`, below. |

`output_format`:

| Field | Values | Notes |
|---|---|---|
| `container` | `raw`, `wav`, `mp3` | Streaming (SSE and WebSocket) accepts only `raw`. |
| `encoding` | `pcm_f32le`, `pcm_s16le`, `pcm_mulaw`, `pcm_alaw` | Required for `raw` and `wav`; omit for `mp3`. Mono. |
| `sample_rate` | `8000`, `16000`, `22050`, `24000` | Default 24000. |

Without `output_format` the whole-file endpoint returns WAV and the stream sends
32-bit float PCM, both at 24 kHz. Unknown fields anywhere in the body are refused
with 422. The server is the only validator (see §6.4).

WebSocket requests add an optional `context_id`: 1–128 characters of
`A-Z a-z 0-9 . _ -`.

### Whole-file response

`200` with the audio as the body. With `output_format` the response carries
`X-Audio-Format` (the encoding, or `mp3`), `X-Audio-Sample-Rate` and
`X-Audio-Channels` (both absent for mp3). `Content-Type` is `audio/wav`,
`audio/mpeg`, `audio/pcm` (s16/f32 raw), `audio/basic` (μ-law raw) or
`application/octet-stream` (A-law raw). Every response has `X-Request-Id`.

### Stream response (SSE)

`200`, `text/event-stream`, `X-Request-Id`, plus the `X-Audio-*` headers when the
request has an `output_format` (without one the stream is 32-bit float PCM at
24 kHz and the headers are absent; clients report encoding and sample rate as
unknown rather than guessing). Events:

```
event: chunk
data: {"type":"chunk","status_code":206,"done":false,"data":"<base64 audio>","step_time":0.0123}

event: done
data: {"type":"done","status_code":200,"done":true}
```

or, ending a stream that already sent audio:

```
event: error
data: {"type":"error","status_code":502,"message":"…","error_code":"stream_interrupted","retryable":true,"request_id":"req_…"}
```

A failure before the first chunk is an ordinary HTTP error (§3), not an event.

### WebSocket messages

Client → server: a request body plus `context_id`, or `{"context_id":"…","cancel":true}`.
Server → client, each with `type`, `done`, and `context_id`/`request_id` when known:

| `type` | `done` | Meaning |
|---|---|---|
| `chunk` | false | `data` is base64 audio of one request |
| `done` | true | that request finished |
| `error` | true | that request failed; `status_code` is the HTTP status it would have had, plus the error fields of §3 |
| `cancelled` | true | answer to a cancel; exactly one per cancel, after everything the context had sent before it |

Requests with the same `context_id` run one after another in the order sent;
different contexts run at the same time and their messages interleave. A refusal
keeps its place in its context's order. A message about no request at all (for
example a binary frame) is an `error` with neither `context_id` nor `request_id`.

Per-connection limits: 16 contexts generating at once, 64 requests accepted and
not finished, 256 KiB per message. The server pings every 30 s, closes after 5
minutes with nothing in progress (close 1000), and closes with 1001 when it is
being restarted or taken out of rotation, after finishing what it accepted.

---

## 2. One vocabulary

The same concept has the same name everywhere. Request fields keep their wire
names (snake_case) in every language, JavaScript included, so code, docs, curl
and MCP tool arguments read the same. Client options are idiomatic per language.

| Concept | Python | JavaScript | MCP | LiveKit |
|---|---|---|---|---|
| Client | `Bakbak()`, `AsyncBakbak()` | `new Bakbak()` | — | `bakbak.TTS()` |
| Whole file | `client.tts.generate(...)` | `client.tts.generate({...})` | tool `text_to_speech` | `tts.synthesize()` |
| SSE stream | `client.tts.stream(...)` | `client.tts.stream({...})` | — | `transport="http"` |
| WebSocket | `client.tts.websocket()` | `client.tts.websocket()` | — | default transport |
| A context | `ws.context(context_id=None)` | `ws.context({contextId})` | — | one per stream attempt |
| Send one request | `ctx.send(text=..., ...)` | `ctx.send({text, ...})` | — | per sentence |
| Send an LLM stream | `ctx.send_stream(tokens)` | `ctx.sendStream(tokens)` | — | per sentence |
| No more input | `ctx.end_input()` | `ctx.endInput()` | — | — |
| Cancel | `ctx.cancel()` | `ctx.cancel()` | — | on interruption |
| Events | `ctx.events()` | `ctx.events()` | — | — |
| Audio only | `ctx.audio()` | `ctx.audio()` | — | — |
| Voices | `client.voices.list(model=, language=)` | `client.voices.list({model, language})` | tool `list_voices` | `tts.list_voices()` |
| Sentence splitter | `bakbak.text.split_sentences` | `splitSentences` | (internal) | `bakbak.SentenceTokenizer` |

Environment variables (read when the client is constructed, never at import):

| Variable | Meaning |
|---|---|
| `BAKBAK_API_KEY` | API key, when none is passed. The LiveKit plugin also falls back to `RAYA_API_KEY`. |
| `BAKBAK_BASE_URL` | Base URL override, e.g. a staging deployment. |

A client constructed with no key and no `BAKBAK_API_KEY` **must** raise a
`BakbakError` that names the argument and the variable. (The MCP server is the
exception: it starts, and every tool call returns that message as a tool error.)

---

## 3. Errors

### 3.1 Classes

Every client exposes this hierarchy under these names (Python and JavaScript
alike; the LiveKit plugin maps onto LiveKit's own classes, §9):

```
BakbakError                         anything raised by the SDK
├── APIError                        the service answered, or a stream broke
│   ├── APIStatusError              an HTTP status or an in-band error before audio
│   │   ├── BadRequestError             400
│   │   ├── AuthenticationError         401
│   │   ├── InsufficientCreditsError    402
│   │   ├── PermissionDeniedError       403
│   │   ├── NotFoundError               404
│   │   ├── RequestTooLargeError        413
│   │   ├── UnprocessableEntityError    422
│   │   ├── RateLimitError              429
│   │   └── InternalServerError         ≥ 500
│   └── StreamInterruptedError      failed after audio was delivered
└── APIConnectionError              no usable answer: network, TLS, DNS
    ├── APITimeoutError
    └── WebSocketClosedError        the WebSocket closed and a request could not continue
```

Any other status maps to `APIStatusError` itself.

Every `APIError` **must** carry:

| Attribute | Meaning |
|---|---|
| `status` | HTTP status, or the event's `status_code`. `None`/`undefined` for a truncated stream. |
| `error_code` | The body's `error_code` if present, else none. Never invented by the client. |
| `message` | A sentence to show a person (§3.2). |
| `retryable` | Whether sending the same request again can succeed (§3.3). |
| `request_id` | `X-Request-Id` if present, else the body's `request_id`, else the event's. |
| `body` | The parsed JSON body (or event), so extra fields such as `required_inr` stay readable. |

`StreamInterruptedError` also carries `audio_bytes`: how much audio was delivered
before the failure. Python names attributes in snake_case; JavaScript uses
camelCase properties (`errorCode`, `requestId`, `audioBytes`) and keeps `body`
as received.

Neither the message, `repr`/`toString`, nor any attribute of an error may contain
the API key or request headers.

### 3.2 The message

First non-empty of: the body's `message` (string); the body's `detail` when it is
a string; the body's `detail` list rendered (below); the body's `error`; the HTTP
reason phrase; `"HTTP <status>"`.

Rendering a `detail` list: each entry becomes `<loc>: <msg>`, where `<loc>` is the
entry's `loc` without a leading `"body"`, joined with `.`; an entry with no `loc`
is just its `msg`; a string entry is itself; an entry with no `msg`, and any empty
rendering, is skipped. Trailing whitespace of `message` is ignored when checking
for `.`, `!` or `?`. The
entries are joined with `"; "`, e.g. `voice_id: Field required;
output_format.encoding: Value error, …`.

When the message is the body's `message` and the body also has a `detail` list
whose rendering is not already a substring of it, the rendering is appended:
`"<message> <rendering>"` if the message ends with `.`, `!` or `?`, else
`"<message>: <rendering>"`. A generic sentence ("The request is malformed or
invalid.") would otherwise hide which field is wrong.

### 3.3 Retryable

The body's or event's `retryable` when it is a boolean. Otherwise by status:
true for 408, 429, 500, 502, 503 and 504; false for everything else.
Connection errors and timeouts before any response are retryable.

### 3.4 Shapes the client must accept

All three, from the same endpoints (fixtures cover each):

1. Current: `{"detail": … , "error_code", "message", "retryable", "request_id"}`,
   plus `error`/`required_inr`/`available_inr` on 402 and
   `error`/`concurrency_limit` on 429.
2. Older Router releases: `{"detail": "<sentence>"}` or `{"detail": [<entries>]}`.
3. The API gateway (wrong key, unknown path, body over 256 KB):
   `{"message": "…", "request_id": "<gateway id>"}` with no `X-Request-Id`.

---

## 4. Retries, timeouts and the rule about audio

### 4.1 The rule

**Never retry once audio has been delivered** — not after the first chunk of a
stream, and not after the first byte of a whole-file body. The request is charged
once audio has left the service, and a retry would play the start twice. Raise
`StreamInterruptedError` instead, with its `request_id` and `audio_bytes`.

### 4.2 When to retry (before any audio)

- Connection errors and timeouts before any response.
- An `APIStatusError` whose `retryable` is true.
- An SSE `error` event, or a WebSocket `error`, that arrives before any audio of
  its request and is retryable.

At most `max_retries` retries (default **2**), so at most 3 attempts.

### 4.3 How long to wait

- If the HTTP response has `Retry-After` (seconds, or an HTTP date): exactly that,
  capped at 60 s.
- Otherwise retry *n* (1-based) waits `min(0.5 × 2^(n−1), 8) × (1 − 0.25 × r)`
  seconds, with `r` uniform in [0, 1): 0.375–0.5 s, then 0.75–1.0 s, then
  1.5–2.0 s.

Implementations **must** make the sleep function injectable so that tests can
record the waits; fixtures list them as `[min, max]` pairs.

### 4.4 Timeouts

| Option | Default | Meaning |
|---|---|---|
| connect timeout | 5 s | TCP + TLS (+ WebSocket upgrade) |
| read timeout | 60 s | longest wait for the next byte of a response, or the next message of a WebSocket context that has unfinished requests; includes time to first audio |
| total timeout | none | optional per call |

A timeout before any audio raises `APITimeoutError` (retryable); after audio,
`StreamInterruptedError`. Where the platform cannot time connecting separately
(`fetch`), the read timeout covers it. A whole-file call holds no audio until its body starts,
so its read timeout runs from the request to the first byte, then between bytes.

---

## 5. Streams

### 5.1 SSE parsing

- Accept LF and CRLF line ends. Ignore comment lines (starting with `:`), `id:`
  and `retry:` lines. Join multi-line `data:` with `\n`. Dispatch on a blank line.
- The payload's `type` is authoritative; the `event:` name is a hint. Ignore
  unknown types (forward compatibility).
- Decode each chunk's base64 exactly once, into bytes. Never hand base64 to users.
- A stream must end with `done` or `error`. End of body without either, after
  some audio, **must** raise `StreamInterruptedError` (`status` none, `retryable`
  true). Never return a short stream as success. With no audio at all it is a
  connection failure: retried under §4.2, and after the last attempt
  `StreamInterruptedError` with `audio_bytes` 0.
- Use no line-length limit below 16 MiB; one chunk's base64 can be large.

### 5.2 Audio helpers

- `Audio` (whole file) and stream results expose the bytes, `request_id`,
  `content_type` and, when the headers are present, `encoding` and `sample_rate`.
- `save(path)` writes exactly the bytes received.
- `to_wav()` / `toWav()` wraps raw audio in a WAV header: the canonical 44-byte
  PCM header for `pcm_s16le`; for `pcm_f32le` (format 3), `pcm_alaw` (6) and
  `pcm_mulaw` (7) an 18-byte `fmt ` chunk (`cbSize` 0) plus a 4-byte `fact` chunk,
  58 bytes in all. Mono; byte rate and block align from the encoding's sample size.
- Samples never straddle a chunk boundary in what the client hands out: if a chunk
  ends mid-sample (f32 or s16), carry the remainder into the next chunk.
- No playback helpers in the core packages.

---

## 6. WebSocket client

### 6.1 Connection

- Python and Node send the key in the `X-API-Key` header. Browsers put it in the
  query string; that is the only case a key goes in a URL. Any URL that is logged
  **must** have the key replaced by `***`.
- `connect()` is explicit and optional: `await client.tts.websocket()` (or
  `connect()` on the handle) opens the socket so the first request pays no
  handshake. Otherwise the first `send` opens it.
- A refused upgrade raises the §3 error for its status and body (401 →
  `AuthenticationError`).
- The client must always be reading: one reader task routes every message to its
  context. That also answers the server's pings, which libraries only do while
  reading. Per-message compression is off.

### 6.2 Contexts and requests

- `ws.context(context_id=None)` returns a context. A missing id becomes
  `ctx_<32 hex>` from a CSPRNG. An id outside `[A-Za-z0-9._-]{1,128}` raises
  locally before anything is sent.
- `ctx.send(text, voice_id, …)` sends one request, with the context's defaults
  merged under the call's arguments. It returns a request handle whose result
  (`request_id`, `audio_bytes`) can be awaited and whose audio can be iterated.
- `ctx.events()` yields, in order: `Chunk(audio, request)`, `Done(request)`,
  `Failed(request, error)`, `Cancelled(request)`. `ctx.audio()` yields only the
  bytes and raises the first failure. Iteration ends after `end_input()` once every
  request has a terminal event.
- An error ends its request only. Later requests on the context still run.
- `ctx.send_stream(tokens)` takes an iterable or async iterable of text pieces
  (LLM tokens), runs them through the sentence segmenter (§7), and sends each
  sentence as soon as it is final, then calls `end_input()` unless told not to.
  It accepts `str` pieces and objects with a text delta (OpenAI-, Anthropic- and
  Gemini-style stream events), found by duck typing and without importing those
  libraries.
- Back-pressure: with 64 unfinished requests on the connection, or a 17th
  context starting while 16 have unfinished requests, `send` waits; it never
  overruns the server's limits. For more parallelism, open more WebSockets.

### 6.3 Cancel

`ctx.cancel()` sends `{"context_id","cancel":true}` and resolves when the
`cancelled` message arrives. Every unfinished request of the context ends as
`Cancelled`. All of the context's audio that was received but not yet handed to
the caller, including audio of requests that had already finished, and audio
that arrives between the cancel and its acknowledgement, is discarded, never
delivered: a cancel means silence now. A `send_stream` still feeding the context
stops: it sends nothing more and returns. Cleanup that sends the cancel must
still run when the task doing it is itself cancelled (barge-in often lands
while an error is being handled). The context stays usable: a
`send` after `cancel()` resolves runs normally. Cancelling a context with nothing
unfinished resolves at once without sending anything.

### 6.4 Closes, reconnects and resends

Each context is bound to the connection its oldest unfinished request was sent
on; with nothing unfinished it is unbound, and its next request goes to the
current connection, opened if needed. Requests of one context are never in
flight on two connections at once.

| Event | What the client does |
|---|---|
| close 1000 (idle) | Nothing was in progress. Mark closed; the next `send` reconnects. Not an error. (Should a 1000 ever arrive with requests unfinished, treat it as 1001.) |
| close 1001 (going away) | Reconnect at once. Every request with no terminal message and no audio is resent, in its context's order, on the new connection, exactly once. Doesn't count against `max_retries`. |
| a **draining refusal**: status 503 and a `message` containing "Reconnect and send the request again" | Stop sending on this connection; new contexts go to a new one. The refused request is held, with any later requests of its context, and resent in order on the new connection as soon as the context has nothing unfinished on the draining one (at the latest when it closes with 1001), never retried on the draining one. The first draining refusal of a request doesn't count against `max_retries`; a second one is handled as an ordinary retryable error (back off per §4.3, count it, resend on a new connection). Once no context is bound to a draining connection, the client closes it (1000) rather than wait for the server. |
| close 1008 or 1009 | The client broke a rule. Fail every unfinished request with `WebSocketClosedError` (not retryable). |
| any other close or a dropped socket | Requests that had audio fail with `StreamInterruptedError`; those with none are resent on a new connection. The first reconnect is immediate; up to `max_retries` more follow with backoff (§4.3), so at most 1 + `max_retries` reconnect attempts before the unfinished requests fail with `WebSocketClosedError`. |
| a retryable `error` before any audio, not a draining refusal | If no later request of the same context has been sent, wait (§4.3) and resend it on the same context. Otherwise deliver the error: resending would break the context's order. |

`error_code` alone never marks a draining refusal: `service_unavailable` also
means the service cannot check balances, and treating that as a drain would
reconnect in a tight loop for as long as the outage lasts.

Nothing ever waits forever: a connection loss delivers an outcome to every
unfinished request, and so does any failure while reconnecting (an exception of
any kind from the transport fails the waiting requests with
`APIConnectionError`). Closing a connection never waits more than about one
second for an unresponsive peer.

A chunk whose `data` is not valid base64 fails its own request as the stream
breaking at that point (§4.1): `StreamInterruptedError` after audio, otherwise a
retryable error. It never closes the connection.

### 6.5 Messages about no request

An `error` with no `context_id` and no `request_id` goes to an optional
connection-level callback or log, never to a context. It does not close anything.

### 6.6 Resource lifetime

A WebSocket handle may live for days and serve millions of requests. Its memory
must not grow with the number of requests or contexts it has served: a context
with nothing unfinished and nothing queued is released from the connection's
routing table (a later `send` registers it again), and audio is released as soon
as the caller has taken it, through whichever reader. Timers and background
readers never keep a process alive on their own once the caller has closed the
handle (in Node: `unref`).

---

## 7. Sentence segmentation

The segmenter is specified by `conformance/segmenter/reference.py` and tested by
`conformance/segmenter/cases.json`. Every implementation **must**:

- produce exactly each case's `sentences` for its `input` and `options`;
- satisfy the streaming invariant: any way of pushing the input in pieces, then
  flushing, gives the same sentences as splitting it whole (test with one piece,
  one code point per piece, and several random cuts);
- measure lengths in Unicode code points, never UTF-16 units;
- keep enough already-emitted text for look-behind when trimming the streaming
  buffer (see `_keep_from` in the reference), or the invariant breaks after a
  hard cut or before a list marker;
- expose the options `min_chars` (20), `first_min_chars` (1), `max_chars` (400),
  `eager_first_clause` (false) and `first_clause_min_chars` (24), named in each
  language's style.

`max_chars` must never exceed 5,000, the API's limit.

Segmentation is linear in the text's length: the reference splits 200,000
characters, with or without punctuation, in tens of milliseconds, and an
implementation must not be more than a small constant factor slower (no
rescanning of the remaining text per sentence). A streaming implementation in a
UTF-16 language carries a trailing high surrogate over to the next push.

"Speakable" uses Unicode general categories as of Unicode 15.1; characters
assigned in later versions may be classified differently by different runtimes,
and the vectors avoid them.

---

## 8. Identity, logging, privacy

- Python and Node send `User-Agent: <package>/<version> <runtime>/<version> <os>/<arch>`,
  e.g. `bakbak-python/1.0.0 python/3.12.4 linux/x86_64`, and
  `X-Bakbak-Client: <package>/<version>`.
- Browsers send neither. The API's CORS policy allows only `Content-Type`,
  `X-API-Key` and `Authorization`, and an extra header fails the preflight.
- No telemetry and no update checks: the clients never call anything except the
  API base URL they were given.
- Logs never contain the API key or synthesis text. Debug logs may contain
  request ids, statuses, timings and sizes. The key never appears in the `repr`
  or `toString` of a client or options object either.
- An error never carries the key or request headers anywhere reachable from it,
  including chained causes (`__cause__`, `__context__`, `cause`) and the
  third-party exceptions they wrap: drop or sanitise such causes.
- A key that is empty or contains whitespace or control characters is refused
  at construction with a `BakbakError` that says so without echoing the key.
- **Never follow redirects**, on HTTP requests or the WebSocket handshake: the
  API never redirects, and following one would hand `X-API-Key` (a custom header
  that `fetch` and several WebSocket libraries keep on cross-origin hops) to
  another origin. A 3xx is returned to the caller as an `APIStatusError` with its
  status.
- The JavaScript SDK refuses to run in a browser with a raw API key unless the
  caller passes `dangerouslyAllowBrowser: true`, and says why: the key would be
  visible to anyone using the page.

---

## 9. The LiveKit plugin

The plugin follows this spec except where LiveKit decides:

- Errors map to LiveKit's classes: `APIStatusError(status_code, request_id, body,
  retryable=<§3.3>)`, `APITimeoutError`, `APIConnectionError`. LiveKit's base class
  does the retrying, and only before any audio has been pushed.
- It sets `output_format` to `raw`/`pcm_s16le` at the TTS sample rate and emits
  `audio/pcm`.
- It uses LiveKit's shared aiohttp session, not the Python SDK, so agent
  deployments have no second HTTP stack.

---

## 10. Compatibility promises

- Responses: unknown fields and unknown event types are ignored.
- Requests: the client sends the fields the caller gave, plus nothing it invents.
  An `extra_body`/`extraBody` option merges extra fields for anything new on the
  server before the SDK models it.
- Enumerations (languages, models, encodings) are typed as the known values plus
  any string, so a new language needs no SDK release.
- Semantic versioning. Public names in §2 and §3 change only in a major version.

---

## 11. Conformance fixtures

`conformance/http/*.json` and `conformance/ws/*.json` hold scenarios recorded from
the real service (and, for the newest error shapes, from its error contract).
`conformance/README.md` describes their format. Each repository runs every
scenario against a local mock server; a scenario a client cannot express (for
example a WebSocket scenario in a client with no WebSocket API) is skipped with
a reason, never deleted.

`conformance/MANIFEST.sha256` lists every file in `conformance/`. CI checks that
the local copy matches its manifest, and that the manifest matches the other
public repositories' copies.
