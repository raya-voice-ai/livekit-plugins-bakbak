# Contributing

Thanks for helping improve the Bakbak plugin for LiveKit Agents.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/raya-voice-ai/livekit-plugins-bakbak.git
cd livekit-plugins-bakbak
uv sync                   # creates .venv with the package and the dev tools
```

## Checks

Everything CI runs, locally:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

`pytest` runs the unit tests, the conformance scenarios, and the LiveKit-level
tests against local fake servers. Nothing leaves your machine.

To test against the oldest supported LiveKit as well:

```bash
uv run --with "livekit-agents==1.8.7" pytest
```

### Live tests

`tests/test_live.py` talks to a real Bakbak deployment and spends credits. It is
skipped unless both variables are set, and excluded from the default run:

```bash
BAKBAK_API_KEY=... BAKBAK_BASE_URL=https://... uv run pytest -m live -s
```

Run them one at a time (never in parallel): a deployment may serve one request
per model at a time. Never commit keys, `.env` files, hostnames of non-public
deployments or customer text.

## Conformance

`conformance/` holds the behaviour specification shared by every official Bakbak
client (`BEHAVIOR.md`) and the fixtures that check it. It is synced from a
canonical copy. **Do not edit it here.** CI checks it against its manifest:

```bash
cd conformance && sha256sum -c MANIFEST.sha256
```

A scheduled workflow compares the manifest with the other official clients and
warns on drift. To take a new version, copy the canonical `conformance/` over
this one, then run the suite. If the specification seems wrong, raise it where
the canonical copy lives instead of working around it here.

How the scenarios map onto LiveKit's error classes and retry behaviour is
described at the top of `tests/conformance_support.py`.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `perf:`, `docs:`, `test:`, `ci:`, `chore:`. The release notes and the next
  version are computed from them.
- Keep commits small and logical, and add or update tests with every change.

## Releases

Releases are automated with [release-please](https://github.com/googleapis/release-please):

1. Commits on `main` keep a release pull request up to date, with the next version
   in `pyproject.toml` and `CHANGELOG.md`.
2. Merging that pull request tags the commit and publishes a GitHub release.
   Until 1.0.0 these are release candidates (`1.0.0rc2`, `1.0.0rc3`, …) and are
   marked as pre-releases.
3. The release triggers `publish.yml` on its tag. It refuses a tag that is not on
   `main`, builds the package, waits for a maintainer to approve the `pypi`
   environment, and uploads it to PyPI through trusted publishing, with
   attestations. No API token is stored.

To release 1.0.0 itself, add `Release-As: 1.0.0` to the footer of a commit on
`main`, and switch `prerelease`/`versioning` off in `release-please-config.json`.
