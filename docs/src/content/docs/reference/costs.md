---
title: Cost model
description: Napkin math for what an instance costs on Fly.io and Tigris, and which settings move the numbers.
---

An instance costs money in two places: the **Fly Machine** that runs Vaultwarden, and the **S3 bucket** that holds the
database replica and files. For a personal or family vault both are small. Most of the S3 bill comes from request
counts, not storage, so this page mostly counts requests.

:::note
Prices are as published by [Fly.io](https://fly.io/pricing/) and [Tigris](https://www.tigrisdata.com/pricing/) in
October 2026. Check the current pricing pages before relying on the numbers.
:::

## Fly.io

A `shared-cpu-1x` machine with 256 MB of memory costs about **2 USD a month** if it runs around the clock. The exact
price depends on the region. With `auto_stop_machines = "stop"` (the default in `fly.example.toml`), Fly.io stops the
machine when there is no traffic. You only pay for the time it runs, plus a small amount for its root filesystem while
it is stopped.

**Small invoices on personal accounts.** Fly.io has, so far, waived monthly invoices below 5 USD for the *personal*
organization of an account (the one created when you sign up). This is an informal policy, not a documented
guarantee, and it doesn't apply to additional organizations. See the
[community forum](https://community.fly.io/t/does-fly-io-still-waive-5-invoices/25651) for details. If you run
Vaultwarden in your personal organization and nothing else pushes the invoice above 5 USD, the Fly.io side can cost
nothing.

Keep the shared IPv4 address that `fly deploy` assigns by default. A dedicated IPv4 address is billed separately.

## Tigris

| Item | Price | Free each month |
| --- | --- | --- |
| Storage (standard tier) | 0.02 USD per GB-month | 5 GB |
| Class A requests (PUT, COPY, POST, LIST) | 0.005 USD per 1,000 | 10,000 |
| Class B requests (GET, HEAD and others) | 0.0005 USD per 1,000 | 100,000 |
| DELETE requests, egress | free | |

So, per month:

```text
cost ≈ max(0, ClassA − 10,000) × 0.005 / 1,000
     + max(0, ClassB − 100,000) × 0.0005 / 1,000
     + max(0, GB − 5) × 0.02
```

A Vaultwarden database is a few MB, and the Litestream replica only keeps 24 hours of history, so storage stays inside
the free 5 GB unless you store a lot of attachments. **Class A requests are the number to watch:** the free tier
covers 10,000, which is one every 4.3 minutes over a 30-day month.

## Where the requests come from

These numbers assume the machine runs for a full 30-day month (2,592,000 seconds) with the default settings. If it
is stopped part of the time, scale them down to the time it actually runs.

| Source | What it does | Class A per month |
| --- | --- | --- |
| S3 mount monitor, write check | One small PUT every `GEESEFS_MONITOR_INTERVAL` (30 s) | 86,400 |
| S3 mount monitor, listing | Lists `/mnt/s3` every 30 s. GeeseFS may answer from its cache, so this is an upper bound. | up to 86,400 |
| Litestream sync | At most one PUT per `LITESTREAM_SYNC_INTERVAL`, and only when the database changed | see below |
| Litestream retention check | A few LISTs every `LITESTREAM_RETENTION_CHECK_INTERVAL` (1 h) | ~2,000 |
| Litestream snapshot | One PUT of the whole database per `LITESTREAM_RETENTION` (24 h) | ~30 |

Class B requests come from Litestream's validation (a restore every 12 hours), from restoring the database when the
machine starts, and from reading attachments. For a personal vault they stay well inside the free 100,000.

### Litestream sync interval

Litestream checks for new database changes every sync interval. If nothing changed, it makes no request. If something
changed, it uploads everything since the last sync as **one** WAL segment. A burst of writes within one interval, such
as a login or a client sync, costs one PUT.

How often a busy instance uploads depends on the interval. In the worst case, with writes in every interval for the
whole month:

| `LITESTREAM_SYNC_INTERVAL` | Max PUTs per month | Max cost |
| --- | --- | --- |
| `1s` (Litestream's own default) | 2,592,000 | ~13 USD |
| `10s` (this image's default) | 259,200 | ~1.30 USD |
| `60s` | 43,200 | ~0.22 USD |

A household vault never gets near the worst case: a few hundred write bursts a day comes to well under 10,000 PUTs a
month. The default of 10 seconds is a **deliberate cost cap**. It limits what a busy or misbehaving client can cost,
in exchange for a slightly larger window of changes that can be lost if the machine dies without shutting down
cleanly. Set `LITESTREAM_SYNC_INTERVAL=1s` to get Litestream's default back.

### Putting it together

| Scenario | Class A per month | Tigris cost |
| --- | --- | --- |
| Always on, defaults | ~90,000 to ~185,000 | ~0.40 to ~0.90 USD |
| Always on, `GEESEFS_MONITOR_WRITE_CHECK=false` | ~2,000 to ~100,000 | 0 to ~0.45 USD |
| Auto-stopped, running ~2 hours a day | ~8,000 to ~15,000 | 0 to ~0.03 USD |

The S3 mount monitor accounts for most of the requests on an always-on machine. Turning off its write check removes
86,400 PUTs a month, and the monitor still detects a dead or hung mount through the listing. Raising
`GEESEFS_MONITOR_INTERVAL` reduces both parts.

## Scheduled backups

The [backup worker](../../guides/backups/) needs the machine running to meet its schedule, and it writes to a second
bucket. Per hourly capture it makes a handful of Class A requests (listing manifests, listing files, uploading the
archive and manifest) and one GET per attachment. That's about 3,600 Class A requests a month, plus Class B requests
that grow with the number of attachments.

Storage is the bigger factor here. Every archive is a full copy, so a 50 MB vault captured hourly and kept for 30 days
adds up to 36 GB. Set a lifecycle rule on the destination bucket, or raise `BACKUP_INTERVAL_SECONDS`.
