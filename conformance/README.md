# Conformance fixtures

Shared test data for every official Bakbak TTS client. `BEHAVIOR.md` is the
specification; these files check it. Do not edit them in a client repository:
they are generated from recordings of the real service and copied into each
repository by a sync script, and CI fails if a copy drifts from
`MANIFEST.sha256`.

- `segmenter/cases.json`: sentence segmentation vectors; `segmenter/reference.py` is the oracle.
- `http/*.json`: whole-file, SSE stream and voice-list scenarios.
- `ws/*.json`: WebSocket scenarios.

The test API key in every scenario is `test-key`. Point the client at the mock
server's base URL, with `max_retries` 2 and an injected sleep that records its
argument instead of sleeping.

## Matching rules

- **Subset match** (`expect_request.headers`, `recv`): every key in the expected
  object must be present in the actual one with an equal value. Extra keys in the
  actual object are allowed, except in a request body (`expect_request.json`),
  which must be **exactly** equal: clients send only what the caller gave.
- Header names are lower case. Compare them case-insensitively.
- `sleeps`: the waits the client asked for, in order, each within `[min, max]`
  seconds. An empty list means the client must not have slept.
- An `error` object is matched on `class` (the error's class name), then each
  other key present: `status`, `error_code`, `retryable`, `request_id` are equal
  (`null` means absent); `message` equals the error's message; `message_contains`
  is a substring of it; `audio_bytes` equals the audio delivered before the
  failure; `body_fields` is a subset match against the error's parsed body.

## HTTP scenarios (`http/*.json`)

```json
{
  "id": "err_503_capacity_then_ok",
  "description": "…",
  "call": {"method": "generate" | "stream" | "voices.list", "params": { … }},
  "exchanges": [
    {
      "expect_request": {"method": "POST", "path": "/v1/text-to-speech/stream",
                         "headers": {"x-api-key": "test-key", "content-type": "application/json"},
                         "json": { … exact body … }},
      "response": { … one of the forms below … }
    }
  ],
  "expect": {"result": { … } | "error": { … }, "sleeps": [[1.0, 1.0]]}
}
```

`call.params` are the request fields in wire names. `voices.list` params are the
client-side filters `model` and `language`.

Each exchange is one HTTP attempt, in order. The client must make exactly as
many attempts as there are exchanges. Response forms:

| Form | Mock server behaviour |
|---|---|
| `{"status", "headers", "json"}` | that status and headers, the JSON body |
| `{"status", "headers", "sse"}` | that status and headers, the text body exactly as given, then end the body |
| `{"status", "headers", "body_base64"}` | that status and headers, the decoded bytes |
| `{"network_error": true}` | drop the connection without a response |

`expect.result` keys, all optional, checked when present:

| Key | Meaning |
|---|---|
| `audio_bytes`, `audio_sha256` | the whole audio (whole file, or decoded chunks concatenated) |
| `chunks` | number of audio chunks the stream yielded |
| `request_id` | the result's request id |
| `content_type`, `encoding`, `sample_rate` | metadata from the response headers |
| `count`, `ids` | for `voices.list`: how many voices, and their ids in order |

## WebSocket scenarios (`ws/*.json`)

```json
{
  "id": "ws_cancel",
  "connections": [ {"expect_headers": {…}, "script": [ … ]} | {"reject": {…}} ],
  "client": [ … operations … ],
  "expect": {"contexts": {"c1": [ … one outcome per request … ]},
             "connections": 1, "connection_notices": 0, "sleeps": []}
}
```

**Server side.** The mock accepts connections in order; the n-th connection runs
`connections[n]`. A `reject` entry refuses the upgrade with that HTTP `status`,
`headers` and `json` body. A script runs these steps in order:

| Step | Mock server behaviour |
|---|---|
| `{"recv": {…}}` | wait (up to 5 s) for the next client text message; it must be a JSON object that subset-matches |
| `{"send": {…}}` | send this JSON object as a text message |
| `{"close": {"code", "reason"}}` | close the connection with that code |
| `{"expect_no_more": true}` | for 300 ms, no further client message may arrive; then keep the connection open until the client is done |

A connection with an `expect_headers` must have received those headers on the
upgrade request. A `recv` timing out, a mismatch, or an extra connection fails
the scenario.

**Client side.** Run the operations in order:

| Operation | Client action |
|---|---|
| `{"op": "connect"}` | open the WebSocket explicitly |
| `{"op": "send", "context": "c1", "request": {…}}` | `ctx.send` on the context with that id, with exactly these fields |
| `{"op": "wait_audio", "context": "c1"}` | wait until the context has delivered at least one audio chunk |
| `{"op": "wait_done", "context": "c1"}` | wait until every request sent so far on the context has a terminal event |
| `{"op": "cancel", "context": "c1"}` | `ctx.cancel()` and wait for it to resolve |
| `{"op": "wait_closed"}` | wait until the client has seen the server close the connection |

After the last operation, end input on every context and collect each context's
events until all its requests are terminal.

**Expectations.** `contexts` maps each context id to one outcome per request, in
the order sent:

| Outcome | Keys |
|---|---|
| `{"outcome": "done", …}` | `audio_bytes`, `audio_sha256`, `request_id` |
| `{"outcome": "cancelled", …}` | `audio_bytes` delivered before the cancel |
| `{"outcome": "error", …}` | `audio_bytes`, `error` (matched as above) |

`connect_error` is the error `connect` must raise. `connections` is the number of
WebSocket connections the client opened. `connection_notices` is how many
messages about no request reached the connection-level callback.
