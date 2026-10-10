---
title: Architecture
description: How Vaultwarden's data survives on a machine with an ephemeral disk.
---

Fly Machines without a volume lose their disk on every restart. Vaultwarden expects a persistent `/data` directory
(see [Backing up your vault](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault)). This image
stores each part of that directory somewhere else.

## Where the data lives

| Path | Persistence |
| --- | --- |
| `/data/db.sqlite3` | Restored on startup and continuously replicated to `vaultwarden.db/` in the bucket by [Litestream](https://litestream.io/), encrypted with `AGE_SECRET_KEY`. |
| `/data/attachments` | Redirected to `/mnt/s3/attachments`. |
| `/data/sends` | Redirected to `/mnt/s3/sends`. |
| `/data/icon_cache` | Redirected to `/mnt/s3/icon_cache`. |
| `/data/config.json` | Generated from environment variables on every start, then made read-only. |
| `/data/rsa_key.pem`, `/data/rsa_key.pub.pem` | Written from `VAULTWARDEN_RSA_PRIVATE_KEY` on every start. |

`/mnt/s3` is a [GeeseFS](https://github.com/yandex-cloud/geesefs) mount of the `data/` prefix in the same bucket.
Vaultwarden encrypts attachments and Sends itself, so the objects are stored without extra server-side encryption.

```text title="Bucket layout"
<bucket>/
├── vaultwarden.db/      Litestream replica (age-encrypted)
├── data/
│   ├── attachments/
│   ├── sends/
│   └── icon_cache/
└── import-db.sqlite     only during a migration (see IMPORT_DATABASE)
```

## Startup sequence

1. Mount the bucket at `/mnt/s3` with GeeseFS (if `GEESEFS_ENABLED`).
2. Write the RSA key pair from `VAULTWARDEN_RSA_PRIVATE_KEY`.
3. Generate `config.json` from `VAULTWARDEN_*` variables and check that it is valid JSON.
4. Idle here if `ENTRYPOINT_IDLE=true`.
5. Restore the database with Litestream, or load the database from the bucket key named by `IMPORT_DATABASE`, if it is set.
6. Start Vaultwarden under Litestream replication, plus the S3 monitor and the
   [backup worker](../../guides/backups/) if enabled.

If a step fails, the entrypoint exits, or idles if `ENTRYPOINT_IDLE=true`, so you can investigate with
`fly ssh console`.

## S3 mount monitor

A FUSE mount can hang or disappear without Vaultwarden noticing. The monitor runs next to Vaultwarden and checks
`/mnt/s3` every `GEESEFS_MONITOR_INTERVAL` seconds:

- Is the GeeseFS process running, and is `/mnt/s3` still mounted? If not, the mount is **broken** immediately.
- Can `/mnt/s3` be listed within `GEESEFS_MONITOR_TIMEOUT`? With `GEESEFS_MONITOR_WRITE_CHECK`, can a small probe
  file be written, fsynced and read back? A failure counts toward `GEESEFS_MONITOR_FAILURE_THRESHOLD`.

When the mount is considered broken, the monitor logs an error and sends `SIGTERM` to Litestream and Vaultwarden.
After 60 seconds it sends `SIGKILL`, and the container exits with status `1`. Fly.io then restarts the machine
according to its restart policy (`on-failure` by default), which mounts the bucket again.

:::note[S3 outages don't trigger restarts]
Before restarting, the monitor checks with `mc` that the bucket itself is reachable. If it isn't, for example during
an outage of the S3 provider, the machine is **not** restarted. The disk doesn't survive a restart, so Litestream
could neither upload its pending changes nor restore the database. Vaultwarden keeps serving from the local database,
and the restart happens once S3 is reachable again, if the mount is still broken by then.
:::

A deliberate stop (for example `fly machine stop` or auto-stop) turns the monitor off first, so a check that fails
during shutdown can't cause a restart.
