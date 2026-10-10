---
title: Introduction
description: What this project is, what it costs, and the trade-offs it makes.
---

This project packages [Vaultwarden](https://github.com/dani-garcia/vaultwarden), the lightweight Bitwarden-compatible
server, into a container image built for [Fly.io](https://fly.io/). Fly Machines have an ephemeral disk unless you
attach a volume. Instead of a volume, the image keeps all state in an S3 bucket (for example
[Tigris](https://www.tigrisdata.com/), which Fly.io provisions for you):

- The **SQLite database** is restored with [Litestream](https://litestream.io/) when the machine boots and
  replicated back to the bucket every few seconds. Replicas are encrypted with an
  [age](https://github.com/FiloSottile/age) key.
- **Attachments, Sends and the icon cache** are stored in the same bucket and mounted into the machine with
  [GeeseFS](https://github.com/yandex-cloud/geesefs).
- **Configuration and keys** come from environment variables and Fly secrets, so the machine holds nothing that
  can't be rebuilt.

See [Architecture](/vaultwarden-fly-io/reference/architecture/) for the details.

## Cost

On the smallest VM size (`shared-cpu-1x`) with the Tigris free tier, an instance costs about **2 USD a month**,
depending on the region. A personal or family instance is idle most of the time, and Fly.io stops the machine while
it is idle, so the real cost is usually lower.

## Trade-offs

:::caution[Single machine only]
Litestream needs exactly one writer, so you must run **one** machine. High availability is not supported.
:::

- **Cold starts.** When Fly.io has stopped the machine, the first request starts it again: Litestream restores the
  database, then Vaultwarden starts. The first request takes a few seconds longer.
- **The admin panel is read-only.** `config.json` is regenerated from environment variables on every start. Change
  settings in `fly.toml` or with `fly secrets set`, not in the admin panel.
- **Replication window.** Litestream pushes changes every 10 seconds by default. If the machine is killed hard, you
  can lose the changes made since the last push.
