---
title: How it works
description: Where the data lives, what happens on start, and how backups are made.
---

Vaultwarden keeps everything in a `/data` folder. A Fly.io machine without a volume loses that folder on every
restart, so this image stores each part of it somewhere else.

## Where the data lives

| Path | Where it really is |
| --- | --- |
| `/data/db.sqlite3` | Downloaded on start. Litestream uploads changes to `vaultwarden.db/` in the bucket. |
| `/data/attachments`, `/data/sends`, `/data/icon_cache` | In the bucket's `data/` folder, mounted at `/mnt/s3` with GeeseFS. |
| `/data/config.json` | Built from environment variables on every start. Read-only. |
| `/data/rsa_key.pem` | Written from `VAULTWARDEN_RSA_PRIVATE_KEY` on every start. |

## What happens on start

1. Mount the bucket at `/mnt/s3`.
2. Write the RSA key and build `config.json`.
3. Download the database, or load `IMPORT_DATABASE` if set.
4. Start Vaultwarden, Litestream, the S3 mount monitor and, if turned on, the backup worker.

If a step fails, the container exits. With `ENTRYPOINT_IDLE=true` it waits instead, so you can look around with
`fly ssh console`.

## S3 mount monitor

The bucket folder can hang without Vaultwarden noticing. Every 30 seconds the monitor lists `/mnt/s3` and writes a
small test file. After 3 failures in a row, it stops the container, and Fly.io starts a fresh one.

If S3 itself is down, the monitor waits instead. A restart then would lose the local database, because it couldn't
be uploaded or downloaded.

## Backups

The [backup worker](../../keep-your-data-safe/turn-on-backups/) makes one backup per interval. Each backup:

1. copies the database while Vaultwarden runs, and checks it,
2. downloads attachments and Sends from the app bucket,
3. adds the settings and keys needed to restore,
4. encrypts everything with age and uploads it to `<prefix>/archives/`,
5. writes a small JSON file to `<prefix>/completed/` once the upload is checked.

The worker reads these JSON files on start to know when the next backup is due. Failed backups are retried with
growing waits, up to 15 minutes. A failed backup never restarts Vaultwarden.

If some attachments are missing, the backup is still written, marked `degraded`. The archive lists the missing files.
The icon cache and the Litestream history are not saved.
