---
title: What this is
description: What this project does, what it costs, and its limits.
---

This project runs [Vaultwarden](https://github.com/dani-garcia/vaultwarden) on [Fly.io](https://fly.io/).
Vaultwarden is a small server that works with all Bitwarden apps.

A Fly.io machine loses its disk when it restarts. So this project keeps everything in an S3 bucket instead:

- **The database** is copied to the bucket every few seconds by [Litestream](https://litestream.io/). When the
  machine starts, Litestream downloads it again. The copy is encrypted with [age](https://github.com/FiloSottile/age).
- **Attachments and Sends** are stored directly in the bucket. [GeeseFS](https://github.com/yandex-cloud/geesefs)
  makes the bucket look like a normal folder to Vaultwarden.
- **Settings and keys** come from your `fly.toml` and Fly.io secrets.

[How it works](../../reference/architecture/) has the details.

## Cost

About **2 USD a month** for the machine, often less, because Fly.io stops it when it is idle. Storage usually fits
in the free tier. See [Costs](../../reference/costs/).

## Limits

:::caution[Run only one machine]
Two machines would overwrite each other's copy of the database. Vaultwarden itself also expects to run as a single
server.
:::

- **The first request after a break is slow.** If Fly.io has stopped the machine, it needs a few seconds to start
  and download the database.
- **The admin panel can't change settings.** Settings are rebuilt from `fly.toml` and secrets on every start. Change
  them there.
- **A crash can lose the last 10 seconds.** Changes are uploaded every 10 seconds. A normal stop or restart uploads
  everything first. To shorten the window, set `LITESTREAM_SYNC_INTERVAL=1s`. This costs more S3 requests.

## Not only Fly.io

The image also runs on Docker or Kubernetes, with any S3-compatible bucket. See
[Docker and Kubernetes](../../guides/kubernetes/).
