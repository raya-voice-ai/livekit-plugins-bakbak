# Security

## Reporting a vulnerability

Please report security issues privately to **support@getraya.app**, not in a
public issue. Include what you found, how to reproduce it, and the plugin and
`livekit-agents` versions. We will acknowledge your report within three working
days and keep you informed until it is resolved.

If your API key may have been exposed, rotate it right away and tell us at the
same address.

## Supported versions

Security fixes are made for the latest release. Release candidates are
supported until the next release.

## How the plugin handles secrets

- The API key is sent only in the `X-API-Key` header, only to the configured
  base URL, and never placed in a URL.
- Logs, error messages and `repr()` of errors never contain the key or the
  request headers.
- The plugin's logs never contain the text being spoken. Messages from the
  server appear in them only under LiveKit's `lk.pii.*` fields, which LiveKit
  redacts unless you turn them on.
