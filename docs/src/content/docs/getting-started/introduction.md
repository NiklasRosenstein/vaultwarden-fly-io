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

See [Architecture](../../reference/architecture/) for the details.

## Cost

On the smallest VM size (`shared-cpu-1x`), the machine costs about **2 USD a month** if it runs around the clock,
depending on the region. A personal or family vault is idle most of the time, and Fly.io stops the machine while it is
idle, so the real cost is usually lower.

On top of that, Fly.io has so far waived invoices **below 5 USD** for the personal organization of an account. This
is an informal policy rather than a guarantee, but if Vaultwarden is all you run there, the Fly.io bill is often zero.

On the storage side, a vault usually fits in Tigris's free tier (5 GB, 10,000 write and 100,000 read requests a
month). An always-on machine goes a little over the free requests, for well under 1 USD. The
[cost model](../../reference/costs/) breaks down where the requests come from and which settings reduce them.

## Trade-offs

:::caution[Single machine only]
Run exactly **one** machine. Litestream replicates an SQLite database, which has a single writer, and two machines
would overwrite each other's replica. Vaultwarden itself isn't built for several active instances either: its
WebSocket notifications are only delivered within one process, and the maintainers recommend
[active-passive setups](https://vaultwarden.discourse.group/t/running-highly-available-vaultwarden/3285) over
active-active ones. A restarted machine restores from the bucket within seconds, which is enough availability for a
personal vault.
:::

- **Cold starts.** When Fly.io has stopped the machine, the first request starts it again: Litestream restores the
  database, then Vaultwarden starts. The first request takes a few seconds longer.
- **The admin panel is read-only.** `config.json` is regenerated from environment variables on every start. Change
  settings in `fly.toml` or with `fly secrets set`, not in the admin panel.
- **Replication window.** This image pushes database changes every **10 seconds**, where Litestream's own default is
  1 second. The longer interval caps the number of S3 requests, and with it the cost (see the
  [cost model](../../reference/costs/#litestream-sync-interval)). The trade-off: if the machine dies without shutting
  down cleanly, up to 10 seconds of changes can be lost. A normal stop or restart uploads everything first. Set
  `LITESTREAM_SYNC_INTERVAL=1s` if you prefer the shorter window.

## Not only Fly.io

The image runs on any container platform that can mount FUSE, with any S3-compatible bucket. See
[Docker and Kubernetes](../../guides/kubernetes/).
