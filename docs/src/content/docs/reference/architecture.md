---
title: How it works
description: Where the data lives, what happens on start, and how backups are made.
---

Vaultwarden keeps everything in a `/data` folder. A Fly.io machine without a volume loses that folder on every
restart. So this image stores each part of it somewhere else.

## Where the data lives

| Path | Where it really is |
| --- | --- |
| `/data/db.sqlite3` | Downloaded on start. Litestream uploads changes to `vaultwarden.db/` in the bucket, encrypted with `AGE_SECRET_KEY`. |
| `/data/attachments` | In the bucket, through `/mnt/s3/attachments`. |
| `/data/sends` | In the bucket, through `/mnt/s3/sends`. |
| `/data/icon_cache` | In the bucket, through `/mnt/s3/icon_cache`. |
| `/data/config.json` | Built from environment variables on every start. Read-only. |
| `/data/rsa_key.pem`, `/data/rsa_key.pub.pem` | Written from `VAULTWARDEN_RSA_PRIVATE_KEY` on every start. |

`/mnt/s3` is the `data/` folder of the bucket, mounted with [GeeseFS](https://github.com/yandex-cloud/geesefs).
Vaultwarden already encrypts attachments and Sends, so they are stored as they are.

```text title="Bucket layout"
<bucket>/
├── vaultwarden.db/      database copy (encrypted)
├── data/
│   ├── attachments/
│   ├── sends/
│   └── icon_cache/
└── import-db.sqlite     only while moving from another server
```

## What happens on start

1. Mount the bucket at `/mnt/s3`. With an AWS role, first wait up to 30 seconds for the token file.
2. Write the RSA keys.
3. Build `config.json` from the `VAULTWARDEN_*` variables.
4. Stop here and wait, if `ENTRYPOINT_IDLE=true`.
5. Download the database. If `IMPORT_DATABASE` is set, load that file instead.
6. Start Vaultwarden, Litestream, the S3 mount monitor and, if turned on, the backup worker.

If a step fails, the container exits. With `ENTRYPOINT_IDLE=true` it waits instead, so you can look around with
`fly ssh console`.

## S3 mount monitor

The bucket folder can hang or disappear without Vaultwarden noticing. The monitor checks it every
`GEESEFS_MONITOR_INTERVAL` seconds:

- Is GeeseFS running, and is `/mnt/s3` mounted? If not, the mount is broken.
- Can it list `/mnt/s3` in time? Can it write a small test file (if `GEESEFS_MONITOR_WRITE_CHECK` is on)? After
  `GEESEFS_MONITOR_FAILURE_THRESHOLD` failures in a row, the mount is broken.

When the mount is broken, the monitor stops Vaultwarden and Litestream, and the container exits. Fly.io then starts
the machine again, which mounts the bucket again.

:::note[No restart while S3 is down]
Before it restarts, the monitor checks that S3 answers. If S3 itself is down, a restart would lose the local
database, because Litestream couldn't upload or download it. So the monitor waits. Vaultwarden keeps running from the
local database until S3 is back.
:::

A normal stop, such as `fly machine stop` or auto-stop, turns the monitor off first.

## Backups

This section explains the [backup worker](../../guides/backups/) in detail.

### Schedule

On start, the worker reads the newest completion file from the backup bucket and waits until the next backup is due.
If there is none, it starts a backup right away.

- Only one backup runs at a time.
- A failed backup is retried after 60 seconds, then after longer waits, up to 15 minutes.
- A backup failure never restarts Vaultwarden.
- If the backup bucket can't be reached, the worker waits and retries. It doesn't assume there are no backups.
- Broken or mismatched completion files are skipped with a warning. Nothing is deleted.

### What a backup does

1. Wait until Vaultwarden answers on `/alive`.
2. Copy the database while it runs, and check it with `PRAGMA integrity_check`.
3. Download `data/attachments/` and `data/sends/` from the app bucket.
4. Check that every file the database refers to is there and has the right size.
5. Add the settings and keys needed to restore (see [What gets saved](#what-gets-saved)).
6. Pack everything into a `.tar.gz`, encrypt it with age, and upload it.
7. Check the upload, then write a completion file.

A completion file is only written after its archive is uploaded and checked. A failed attempt can leave an archive
without a completion file behind.

**Missing files.** If some attachments are missing, the worker still writes the backup, marked `degraded`. The
archive lists the missing files. Old, unfinished uploads can leave such gaps, and they shouldn't block backups of
everything else.

**Not saved:** the icon cache and the Litestream history.

:::note[Database and files are not copied at the same instant]
The database copy is consistent. But a file uploaded or deleted during the backup can make it `degraded` or fail.
The size check also can't see a file whose content changed but whose size stayed the same.
:::

### Files in the backup bucket

Paths start with `BACKUP_PREFIX`:

| Path | Contents |
| --- | --- |
| `archives/<UTC-timestamp>-<unique-id>.tar.age` | The encrypted backup |
| `completed/<UTC-timestamp>-<unique-id>.json` | Completion file, not secret, written last |

The completion file has these fields. `CompletionManifestV1` in `backup.py` defines them.

| Field | Meaning |
| --- | --- |
| `schema_version` | Always `1`. |
| `backup_id` | Unique ID of the backup. |
| `captured_at` | When the backup started (UTC). Use this to check that backups are recent. |
| `database_capture_finished_at` | When the database copy finished. |
| `completed_at` | When the upload was checked. |
| `vaultwarden_version` | Vaultwarden version at backup time. |
| `archive_key` | Full key of the archive. |
| `archive_size` | Archive size in bytes. |
| `archive_sha256` | SHA-256 of the archive. |
| `status` | `complete` or `degraded`. |
| `missing_file_count` | Number of missing files. |

### Permissions in detail

- **App bucket** (the app's `AWS_*` credentials): list and read under `data/attachments/` and `data/sends/`.
- **Backup bucket** (`BACKUP_AWS_*` only): list `<prefix>/completed/` and `<prefix>/archives/`, including exact
  archive keys as prefixes (for example `<prefix>/archives/*`). Read and write under both.
- The bucket must support `If-None-Match: *` and `ChecksumSHA256` on uploads.
- If checking an archive returns 403, the worker lists its key to confirm it is really missing.
- Use only one writer per prefix.

### Limits

- **Disk:** the backup is prepared unencrypted in `BACKUP_TMP_DIR`, then encrypted. Have free space of twice the
  backup size. These files are private, but root on the host can read them during a backup.
- **Cleanup:** the files are removed after each attempt. If removal fails, backups pause until the next restart.
- **Time:** `BACKUP_TIMEOUT_SECONDS` covers the whole backup.
- **Size:** at most 4 GiB, because the archive is uploaded in one request.

### What gets saved

The settings saved in a backup are listed in `RECOVERY_FIELDS` in `vaultwarden-fly-io/backup.py`. Every variable on
the [Settings](../configuration/) page must be either saved or listed in `RECOVERY_EXCLUSIONS` with a reason. A test
checks this. Backup settings and one-time flags such as `IMPORT_DATABASE` are not saved.
