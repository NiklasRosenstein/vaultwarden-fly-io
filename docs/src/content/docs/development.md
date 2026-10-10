---
title: Development
description: Run the checks, work on the docs, and how releases are made.
---

## Repository layout

| Path | Contents |
| --- | --- |
| `vaultwarden-fly-io/` | Container image: `Dockerfile`, `entrypoint.sh` and the backup worker `backup.py`. |
| `tests/` | Unit tests for the backup worker. |
| `fly.example.toml` | Starting point for a deployment. |
| `docs/` | This documentation site ([Astro Starlight](https://starlight.astro.build/)). |

## Checks

Install [uv](https://docs.astral.sh/uv/) and `age`, then run from the repository root. uv manages Python 3.12.

```sh
uv sync --locked --group dev --python 3.12
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python -m mypy
uv run --no-sync python -m unittest discover -s tests -v
```

CI runs lint, formatting, strict type checks and tests before it builds the image. Run
`uv run --no-sync ruff format .` to apply formatting.

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. Run `uv lock` after editing them, or
`uv lock --upgrade` to update the resolved versions. SDK stubs and checking tools belong to the `dev` group. The image
installs its runtime dependencies from Alpine packages.

:::tip[Adding an environment variable]
Every variable documented in the [configuration reference](../reference/configuration/) must be
listed in `RECOVERY_FIELDS` or `RECOVERY_EXCLUSIONS` in `backup.py`. A test parses the tables on that page to check
this.
:::

## Documentation

The site is built with Astro Starlight and published to GitHub Pages from the `gh-pages` branch on every push to
`main`. Each pull request that touches `docs/` gets a preview under `pr-preview/pr-<number>/`, linked from a sticky
comment on the pull request and removed when it is closed.

```sh
cd docs
npm ci
npm run dev
```

Pages are in `docs/src/content/docs/`. Link between pages with relative paths (`../guides/backups/`), so the links
also work in previews.

## Releases

Images are published to `ghcr.io/niklasrosenstein/vaultwarden-fly-io`. Renovate opens a pull request for each new
Vaultwarden release. When it is merged, the release workflow tags `<project-version>-vaultwarden-<version>`, creates
a GitHub release and builds the image.
