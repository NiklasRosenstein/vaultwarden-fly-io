# Scheduled recovery backups

Enable the worker with the `BACKUP_*` variables in the [README](../README.md#environment-variables).
It runs alongside Vaultwarden and Litestream; Litestream still provides frequent replication for pod recovery.
Generate a separate recovery identity with `age-keygen -o recovery-key.txt`, keep that file offline, and configure
only its public recipient in the application. Losing this private key makes the archives unrecoverable.

## Capture and publication

Each attempt waits for a successful local `/alive` response, takes an online SQLite snapshot, and requires
`PRAGMA integrity_check` to return `ok`. It downloads `data/attachments/` and `data/sends/` directly from the source
bucket with conditional reads, then checks the presence and byte size of files referenced by the snapshot.
Missing files, changed objects, schema mismatches, and size-limit violations fail the attempt. Retries start a
fresh capture after 60 seconds, doubling up to 15 minutes. The icon cache and Litestream history are not included:
the archive contains a standalone database instead.

SQLite is consistent, but database and file capture are not an atomic snapshot. Pending GeeseFS uploads or
concurrent deletions can fail validation and require a retry. Presence/size checks cannot detect same-size content
changes. Strict cross-resource consistency requires pausing application writes and flushing uploads separately.

The worker streams a gzip tar archive through Age encryption, uploads the ciphertext with a SHA-256 checksum,
and verifies the object's size and checksum metadata before publishing its completion manifest. The destination
must support `If-None-Match: *` and `ChecksumSHA256` on PutObject. Only completed backups advance the schedule.
Failed or interrupted attempts can leave orphan archives, but never claim success before an archive is uploaded.

Paths below are relative to `BACKUP_PREFIX`:

| Path | Contents |
| --- | --- |
| `archives/<UTC-timestamp>-<unique-id>.tar.age` | Encrypted recovery archive |
| `completed/<UTC-timestamp>-<unique-id>.json` | Non-secret completion manifest, published last |

The completion manifest has `schema_version: 1`, `backup_id`, `captured_at`, `database_capture_finished_at`,
`completed_at`, `vaultwarden_version`, `archive_key`, `archive_size`, and `archive_sha256`. Times are UTC RFC3339;
sizes are bytes and SHA-256 is lowercase hex. `archive_key` is the full destination object key. Monitor freshness
using `captured_at`, not upload time, and verify that the referenced archive exists. No monitor or retention
controller is bundled in the image; configure external alerts for stale backups and periodic restore tests.

## Permissions and resource limits

The source credentials need ListBucket for `data/attachments/` and `data/sends/`, plus GetObject under those
prefixes. The destination credentials need ListBucket scoped to `<prefix>/completed/`, GetObject for completion
manifests and archive HEAD checks, and PutObject under `<prefix>/archives/` and `<prefix>/completed/`.
The worker never needs DeleteObject, retention-bypass, or bucket-administration permissions.

Configure destination versioning, Object Lock, and lifecycle retention independently. Keep manifests and their
archives for matching periods. Use one writer per prefix: this worker is not a distributed lock or application
fencing mechanism. Plaintext staging files have private permissions, but the host/root user can read them while
capture runs. Staging is removed after each attempt and on graceful shutdown; hard termination can leave temporary
files until the container's temporary storage is discarded. Budget disk space for the staged payload plus ciphertext.
The timeout bounds the entire attempt, including slow SQLite backups and uploads; backup failure does not stop the app.

## Recovery

1. Select a completed manifest, download its archive, and verify its size and SHA-256 against the manifest.
2. Decrypt and extract into a private directory:

   ```sh
   umask 077
   age -d -i recovery-key.txt -o backup.tar.gz backup.tar.age
   mkdir recovered
   tar -xzf backup.tar.gz -C recovered
   ```

3. The extracted `vaultwarden/` directory contains:
   - `db.sqlite3`: standalone database for the image's database-import workflow or a standard Vaultwarden deployment.
   - `files/attachments/` and `files/sends/`: upload beneath `data/` in a fresh application bucket.
   - `recovery/environment.json`: explicitly selected image inputs, including source storage credentials,
     Litestream Age identity when supplied, and the effective public domain and RSA key.
   - `recovery/config.json` and `recovery/rsa_key.pem`: captured admin configuration and signing key.
   - `manifest.json`: capture metadata and SHA-256/size inventory of the extracted payload files.
4. Validate SQLite and the file inventory. Restore with the recorded Vaultwarden version first, preserve its
   domain/signing key, and adjust storage credentials/endpoints for the recovery destination. Source credentials
   may have expired or been revoked. Reapply any captured admin-UI settings through the corresponding image
   environment options: the image regenerates `config.json` on every start.
5. Verify login and an attachment download in isolation before switching traffic and enabling scheduled backups.
   Keep the original writer fenced and use a separate bucket for recovery testing.

The archive contains application recovery secrets, not an OpenBao snapshot. Backup-destination credentials,
backup settings, and one-time import/idle flags are intentionally excluded. Your recovery private key and access
to the backup destination must be available without the application or its secrets manager.
