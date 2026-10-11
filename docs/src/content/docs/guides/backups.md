---
title: Scheduled backups
description: Write age-encrypted, self-contained archives of your vault to a separate S3 bucket on a schedule.
---

Litestream replicates the database within seconds, but it writes to the same bucket, with the same credentials, as
the running application. The optional **recovery backup worker** adds an independent copy. On a schedule it

1. snapshots the local SQLite database with SQLite's online backup API,
2. downloads attachments and Sends from S3,
3. adds the configuration and secrets needed for recovery,
4. encrypts the result with age and uploads it to a **separate** S3 destination.

Vaultwarden keeps serving requests during the capture. To restore an archive, see [Recovery](../recovery/).

## Enable it

1. Create a recovery identity on a trusted machine and **keep the private key offline**. Don't reuse the
   Litestream `AGE_SECRET_KEY`.

   ```sh
   age-keygen -o recovery-key.txt
   ```

   :::danger
   If you lose `recovery-key.txt`, nobody can decrypt the archives. The application only ever sees the public
   recipient.
   :::

2. Create a destination bucket, and credentials scoped to your prefix (see [Permissions](#permissions)).

3. Set the `BACKUP_*` options on the app:

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

   All options are listed in the [configuration reference](../../reference/configuration/#scheduled-backups).

## Authenticate with OIDC

Instead of long-lived access keys, the worker can exchange an OIDC token (a Fly.io or Kubernetes service account JWT)
for temporary AWS credentials through
[`AssumeRoleWithWebIdentity`](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html):

```
OIDC token file → AWS STS → temporary AWS credentials → backup bucket
```

1. In AWS IAM, add your token issuer as an OIDC identity provider, and create a role that trusts it. Restrict the
   trust policy to your token's audience and subject, and grant the role the [destination permissions](#permissions).
2. Make the token available to the container as a file, for example a projected service account token on Kubernetes.
3. Replace the access keys with:

   ```sh
   BACKUP_AUTH_MODE=web-identity
   BACKUP_AWS_ROLE_ARN=arn:aws:iam::<account>:role/vaultwarden-backup
   BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE=/var/run/secrets/aws/token
   BACKUP_AWS_REGION=<bucket region>
   ```

Credentials are requested from the regional STS endpoint of `BACKUP_AWS_REGION` and refreshed automatically before
they expire. The token file is read again on every refresh, so tokens that are rotated in place keep working.

This mode only works with AWS S3. The worker only uses the `BACKUP_AWS_*` settings: it ignores `AWS_ROLE_ARN`,
`AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ENDPOINT_URL_*`, which belong to the source bucket. Setting static
`BACKUP_AWS_*` keys together with `web-identity` is an error. The source bucket (GeeseFS, Litestream and the backup
worker's reads of attachments) keeps using the application's `AWS_*` access keys.

:::caution[Keep the machine running]
A stopped Fly Machine can't run backups. To meet the interval, set `min_machines_running = 1` or turn off
`auto_stop_machines`.
:::

## Scheduling

On startup the worker reads the latest usable completion record from S3 and waits until the next backup is due. If
no usable record exists, or the interval has already passed, it captures right away.

- Only one capture runs at a time.
- Failed checks or captures are retried after 60 seconds, doubling up to 15 minutes. Vaultwarden is never restarted
  because of a backup failure.
- If the destination is unavailable, the worker never treats that as an empty backup history.
- Malformed completion records, records with future capture times, and records whose archive is missing or doesn't
  match are skipped with a warning. Nothing is deleted.
- Authentication, network and service errors fail the scheduling check and are retried with backoff.

## What a capture does

Each attempt waits for a successful local `/alive` response, takes an online SQLite snapshot, and requires
`PRAGMA integrity_check` to return `ok`. It then downloads `data/attachments/` and `data/sends/` from the source
bucket with conditional reads, and checks that every file the snapshot references is present and has the expected
size.

**Degraded archives.** If referenced files are missing, the worker still writes an archive. It contains the unchanged
database, every file that is available, and the recovery secrets. The encrypted manifest lists the missing paths and
their expected sizes. The public completion record only shows the status and the count. Abandoned uploads can leave
database references without files, so missing files must not block backups of the rest of the vault. Complete and
degraded archives both advance the schedule, so missing files don't trigger a capture on every check.

**Failures.** Download errors, objects that change during download, file-size mismatches, schema mismatches and
size-limit violations fail the attempt. The next attempt starts a fresh capture.

**Not included:** the icon cache and the Litestream history. The archive contains a standalone database instead.

:::note[Consistency]
The SQLite snapshot is consistent, but the database and the files are not captured atomically. Pending GeeseFS
uploads or concurrent deletions can produce a degraded archive or fail an attempt. Presence and size checks can't
detect content changes that keep the same size. For strict consistency across both, pause application writes and
flush uploads before the capture.
:::

## Publication

The worker streams a gzipped tar archive through age encryption and uploads the ciphertext with a SHA-256 checksum.
It verifies the object's size and checksum metadata, and only then publishes the completion manifest. The destination
must support `If-None-Match: *` and `ChecksumSHA256` on PutObject.

A failed or interrupted attempt can leave an orphaned archive behind, but a completion manifest is only published
after its archive is uploaded.

Paths are relative to `BACKUP_PREFIX`:

| Path | Contents |
| --- | --- |
| `archives/<UTC-timestamp>-<unique-id>.tar.age` | Encrypted recovery archive |
| `completed/<UTC-timestamp>-<unique-id>.json` | Non-secret completion manifest, published last |

### Completion manifest

`CompletionManifestV1` in `backup.py` defines the schema. Pydantic validates both the completion manifest and the
encrypted payload manifest before they are written.

| Field | Meaning |
| --- | --- |
| `schema_version` | Always `1`. Other versions are rejected. A new wire format gets a new version and its own decoder. |
| `backup_id` | Unique ID of the capture. |
| `captured_at` | When the capture started (UTC, RFC 3339). Use this field to monitor freshness. |
| `database_capture_finished_at` | When the SQLite snapshot finished. |
| `completed_at` | When the upload was verified. |
| `vaultwarden_version` | Vaultwarden version that produced the data. |
| `archive_key` | Full destination object key of the archive. |
| `archive_size` | Archive size in bytes. |
| `archive_sha256` | Lowercase hex SHA-256 of the archive. |
| `status` | `complete` or `degraded`. |
| `missing_file_count` | Number of referenced files missing from the archive. Consistent with `status`. |

## Monitoring

The image doesn't include a monitor or a retention controller. Set them up outside the app:

- Alert when the newest `captured_at` is too old, and check that the referenced archive exists.
- Alert **separately** when the latest backup is `degraded`. A recent archive protects the database, but not
  necessarily every attachment.
- Restore an archive into an isolated environment from time to time.

## Permissions

**Source credentials** (the app's `AWS_*`): ListBucket on `data/attachments/` and `data/sends/`, and GetObject
under those prefixes.

**Destination credentials** (`BACKUP_AWS_*` access keys or the `BACKUP_AWS_ROLE_ARN` role; the source credentials are
never used for the destination):

- ListBucket scoped to `<prefix>/completed/` and `<prefix>/archives/`. Listing must also allow exact archive keys as
  prefixes, for example `<prefix>/archives/*`.
- GetObject for completion manifests and for HEAD checks on archives.
- PutObject under both prefixes.

If HEAD returns 403, the worker lists the exact key as a prefix to confirm that the archive is really missing before
it skips the record. An archive that exists but can't be read, or a failed listing, stops the check. See
[S3 HeadObject permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html).

The worker never needs DeleteObject, retention-bypass or bucket-administration permissions. Configure versioning,
Object Lock and lifecycle retention on the destination yourself, and keep manifests and archives for the same period.
Use exactly one writer per prefix. The worker doesn't provide a distributed lock or fencing.

## Disk and resource limits

- **Disk:** plaintext staging needs room for the payload plus the ciphertext. Provision at least twice the payload
  size in `BACKUP_TMP_DIR`. Staging files have private permissions, but root on the host can read them while a
  capture runs.
- **Cleanup:** staging is removed after every attempt and on graceful shutdown. A hard kill can leave files behind
  until the container's temporary storage is discarded. If cleanup fails, captures are suspended until restart, so
  plaintext doesn't pile up. Application shutdown is not affected.
- **Timeout:** `BACKUP_TIMEOUT_SECONDS` covers the whole attempt, including slow SQLite backups and uploads. If a worker
  can't be terminated, later captures wait until it exits.
- **Size:** archives are uploaded with a single PUT, so `BACKUP_MAX_BYTES` is capped at 4 GiB.

## What gets captured

The fields captured for recovery are listed explicitly in `RECOVERY_FIELDS` in `vaultwarden-fly-io/backup.py`. Every
option in the [configuration reference](../../reference/configuration/) must either be captured or appear in
`RECOVERY_EXCLUSIONS` with a reason, and CI enforces this. The backup destination options and one-time maintenance flags
are excluded. The worker doesn't export unrelated environment variables and doesn't access a secrets manager.
