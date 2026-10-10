# Scheduled recovery backups

Enable the worker with the `BACKUP_*` variables in the [README](../README.md#environment-variables).
It runs alongside Vaultwarden and Litestream; Litestream still provides frequent replication for pod recovery.
Generate a separate recovery identity with `age-keygen -o recovery-key.txt`, keep that file offline, and configure
only its public recipient in the application. Losing this private key makes the archives unrecoverable.

## Capture and publication

Each attempt waits for a successful local `/alive` response, takes an online SQLite snapshot, and requires
`PRAGMA integrity_check` to return `ok`. It downloads `data/attachments/` and `data/sends/` directly from the source
bucket with conditional reads, then checks the presence and byte size of files referenced by the snapshot.
Missing referenced files produce a **degraded** archive: the database remains unchanged, and the archive includes
all available files and recovery secrets. The encrypted manifest lists missing paths and expected sizes; the public
completion record exposes only status and count. Abandoned uploads can leave database references without files,
so these references must not block backups of the rest of the vault.

Download failures, changed objects, file-size mismatches, schema mismatches, and size-limit violations fail the
attempt. Retries start a fresh capture after 60 seconds, doubling up to 15 minutes. The icon cache and Litestream
history are not included: the archive contains a standalone database instead.

SQLite is consistent, but database and file capture are not an atomic snapshot. Pending GeeseFS uploads or
concurrent deletions can produce a degraded archive or fail an attempt. Presence/size checks cannot detect
same-size content changes. Strict cross-resource consistency requires pausing application writes and flushing uploads separately.

The worker streams a gzip tar archive through Age encryption, uploads the ciphertext with a SHA-256 checksum,
and verifies the object's size and checksum metadata before publishing its completion manifest. The destination
must support `If-None-Match: *` and `ChecksumSHA256` on PutObject. Published archives advance the normal schedule,
including degraded archives; missing files do not cause continuous duplicate captures.
Failed or interrupted attempts can leave orphan archives, but never claim success before an archive is uploaded.

Paths below are relative to `BACKUP_PREFIX`:

| Path | Contents |
| --- | --- |
| `archives/<UTC-timestamp>-<unique-id>.tar.age` | Encrypted recovery archive |
| `completed/<UTC-timestamp>-<unique-id>.json` | Non-secret completion manifest, published last |

The completion manifest has `schema_version: 1`, `backup_id`, `captured_at`, `database_capture_finished_at`,
`completed_at`, `vaultwarden_version`, `archive_key`, `archive_size`, `archive_sha256`, `status` (`complete` or
`degraded`), and `missing_file_count`. Times are UTC RFC3339; sizes are bytes and SHA-256 is lowercase hex. `archive_key` is the full destination object key. Monitor freshness
using `captured_at`, not upload time, and verify that the referenced archive exists. Alert separately when the latest
backup is degraded: a recent archive protects the database but does not guarantee every attachment is recoverable.
No monitor or retention controller is bundled in the image; configure external alerts and periodic restore tests.

`CompletionManifestV1` in `backup.py` defines the version 1 completion schema. The decoder validates required
fields, timestamps, status/count consistency, and checksum format before returning typed metadata. Unsupported
schema versions are rejected; changes to the wire contract require a new version and an explicit decoder.

Scheduling skips malformed completion records, future capture timestamps, and records with missing or mismatched
archives, logging a warning without deleting anything. It uses the next valid record, or captures immediately if
none is valid. Authentication, network, and service errors fail the scheduling check and retry with backoff.

## Permissions and resource limits

The source credentials need ListBucket for `data/attachments/` and `data/sends/`, plus GetObject under those
prefixes. The destination credentials need ListBucket scoped to `<prefix>/completed/` and `<prefix>/archives/`,
GetObject for completion manifests and archive HEAD checks, and PutObject under both prefixes. Listing permissions
must also allow exact archive keys as prefixes (for example `<prefix>/archives/*`). If HEAD returns 403, an exact-prefix
listing must confirm the archive is absent before skipping its record; an existing unreadable archive or failed
listing stops the check. See [S3 HeadObject permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html).
The worker never needs DeleteObject, retention-bypass, or bucket-administration permissions.

Configure destination versioning, Object Lock, and lifecycle retention independently. Keep manifests and their
archives for matching periods. Use one writer per prefix: this worker is not a distributed lock or application
fencing mechanism. Plaintext staging files have private permissions, but the host/root user can read them while
capture runs. Staging is removed after each attempt and on graceful shutdown; hard termination can leave temporary
files until the container's temporary storage is discarded. Budget disk space for the staged payload plus ciphertext.
The timeout bounds the entire attempt, including slow SQLite backups and uploads; backup failure does not stop the app.
If a worker cannot be terminated, further captures wait for its exit. Staging cleanup failures suspend captures until
restart to avoid accumulating plaintext files or exhausting disk; application shutdown still proceeds normally.

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
   - `manifest.json`: capture metadata, SHA-256/size inventory, and `missing_files` (paths and expected sizes).
4. Validate SQLite and the file inventory. For a degraded archive, review `missing_files`; the referenced files
   are unavailable in this archive even though their database records are preserved. Recover those files from
   another archive or source if available. Restore with the recorded Vaultwarden version first, preserve its
   domain/signing key, and adjust storage credentials/endpoints for the recovery destination. Source credentials
   may have expired or been revoked. Reapply any captured admin-UI settings through the corresponding image
   environment options: the image regenerates `config.json` on every start.
5. Verify login and an attachment download in isolation before switching traffic and enabling scheduled backups.
   Keep the original writer fenced and use a separate bucket for recovery testing.

The archive contains application recovery secrets, not an OpenBao snapshot. Backup-destination credentials,
backup settings, and one-time import/idle flags are intentionally excluded. Your recovery private key and access
to the backup destination must be available without the application or its secrets manager.
