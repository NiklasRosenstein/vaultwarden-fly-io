---
title: Turn on backups
description: Keep an extra encrypted copy of your vault in a second bucket.
---

Litestream already copies your database to S3. But that copy sits in the same bucket, with the same credentials, as
the app. If the app or its credentials are compromised, that copy can be lost too.

Backups add a **separate copy**. On a schedule (every hour by default), the app packs the database, attachments,
Sends and the keys needed to restore them into one archive. It encrypts the archive and uploads it to a second bucket.
The app never deletes anything there.

To restore an archive, see [Restore from a backup](../restore-from-a-backup/).

## Set it up

1. **Create a recovery key** on your own computer. Don't reuse `AGE_SECRET_KEY`.

   ```sh
   age-keygen -o recovery-key.txt
   ```

   :::danger
   Keep `recovery-key.txt` offline and safe. Without it, nobody can open the backups. The app only gets the public
   part, which starts with `age1`.
   :::

2. **Create a second bucket**, and credentials that can only read and write in it. See [Permissions](#permissions).

3. **Set the backup options** on the app:

   ```sh
   fly secrets set \
     BACKUP_ENABLED=true \
     BACKUP_BUCKET_NAME=my-backups \
     BACKUP_PREFIX=vaultwarden/ \
     BACKUP_AWS_ACCESS_KEY_ID=... \
     BACKUP_AWS_SECRET_ACCESS_KEY=... \
     BACKUP_AWS_REGION=auto \
     BACKUP_AGE_RECIPIENT=age1...
   ```

   All options are in [Settings](../../reference/settings/#scheduled-backups). For AWS S3 without access keys,
   see [AWS S3 without access keys](../../other-setups/aws-s3-without-access-keys/).

:::caution[Keep the machine running]
A stopped machine can't make backups. Set `min_machines_running = 1` in `fly.toml`, or turn off
`auto_stop_machines`.
:::

## Permissions

The backup credentials need:

- **List** on `<prefix>/completed/` and `<prefix>/archives/*`
- **Read** and **write** under both prefixes

They don't need delete. The bucket must support conditional uploads (`If-None-Match`) and SHA-256 checksums. Set up versioning and an expiry rule on the bucket yourself, so old archives are removed after
a time you choose.

## Check that it works

The app doesn't alert you. Set up checks outside the app:

- **Is the last backup recent?** Each backup writes a small JSON file to `<prefix>/completed/`. Alert when the newest
  `captured_at` is too old.
- **Is the last backup complete?** If some attachments were missing, the backup is still written, with
  `status: degraded`. The database is safe, but some files are not in it. Alert on this separately.
- **Can you restore it?** Try a [restore](../restore-from-a-backup/) in a test bucket from time to time.

[How it works](../../reference/how-it-works/#backups) explains the archive format, the schedule and the limits.
