<p align="center">
    <img src="./.github/assets/vaultwarden-on-flyio.webp">
</p>

# Vaultwarden on Fly.io

Run [Vaultwarden] on [Fly.io] with reliable [Litestream] SQlite replication and attachments/sends stored in S3.

Using the smallest VM size on Fly.io (`shared-cpu-1x`) and leveraging the Tigris object storage free tier, this
costs approx. 2 USD/mo to run (depending on the region). Small Vaultwarden instances won't see traffic 24/7, so
you should pay much less because your VM can be stopped for a large portion of the time.

[Vaultwarden]: https://github.com/dani-garcia/vaultwarden
[Fly.io]: https://fly.io/
[Litestream]: https://litestream.io/

## Prerequisites

- An account on [Fly.io]
- The [fly](https://github.com/superfly/flyctl) CLI
- The [age](https://github.com/FiloSottile/age) CLI

## Installation

1. Create a new Fly.io application

   ```
   $ fly app create <app_name>
   ```

2. Create an S3 object storage bucket for your app.

   ```
   $ fly storage create --app <app_name> --name <app_name>
   ```

3. Create secrets:

   ```
   $ fly secrets set \
       VAULTWARDEN_RSA_PRIVATE_KEY="$(openssl genrsa 2048)" \
       AGE_SECRET_KEY="$(age-keygen | tail -n1)"
   ```

4. Create an admin password, if you want to use the Vaultwarden admin panel. Note that you cannot make any changes
   to the Vaultwarden configuration via the admin panel, because the `config.json` is built entirely from environment
   variables on startup.

   ```
   $ docker run -it --rm ghcr.io/dani-garcia/vaultwarden /vaultwarden hash
   ```

   Because the admin password is already hashed, you can set it in your `fly.toml`'s `[env]` section instead
   of using `fly secrets set`.

5. Create a copy of `fly.example.toml` and update the `app` name.

6. Run `fly deploy`

7. Run `fly scale count 1` (this application does not support high-availability, and by default, the initial
   deployment step sets the machine count to `2`).

## Advanced topics

### Migrating from an existing Vaultwarden installation

First you should install Vaultwarden on Fly.io. Then you should ensure that while you are migrating, no modifications
can be made to your existing Vaultwarden installation. If you can't easily turn off the Vaultwarden installation without
loosing access to the filesystem (e.g. if it is deployed in Kubernetes), make sure to perform a WAL checkpoint on the
SQlite database before downloading it.

    sqlite> PRAGMA wal_checkpoint(TRUNCATE);

Then copy the existing SQlite database to the S3 bucket with the key `import-db.sqlite` and redeploy your app with
`IMPORT_DATABASE` set to `true`. This will make the startup sequence fetch the database from the S3 bucket instead
of restoring the existing backup with Litestream.

    $ mc cp db.sqlite3 tigris/my-vaultwarden-bucket/import-db.sqlite
    $ fly deploy --env IMPORT_DATABASE=true

Once that is complete, check the app logs to ensure that the database was imported from the S3 bucket and that the
Litestream replication has completed. Redeploy your application without the `IMPORT_DATABASE` variable.

    $ fly deploy

Copy your existing Vaultwarden installation's RSA private key to a Fly secret:

    $ fly secrets set VAULTWARDEN_RSA_PRIVATE_KEY="$(cat rsa_key.pem)"

And copy your existing installations' attachments, sends and optionally icon cache to the S3 bucket:

    $ mc cp --recursive attachments sends icon_cache tigris/my-vaultwarden-bucket/data/

Last but not least, check that all relevant configuration options in your existing installations' `config.json`
or environment variables are also set as the corresponding `VAULTWARDEN_*` environment variables or secrets in your
Fly.io app. And that should be it!

### Vaultwarden on ephemeral disk

The [Backing up your Vault](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault) documentation for
Vaultwarden explains the purpose of each the files and directories in the `/data` directory. Since we're running
on an ephemeral disk, we need to have an alternative story around the persistence of these files. This section
describes how the data that would usually live on a persistent disk survives:

[GeeseFS]: https://github.com/yandex-cloud/geesefs/

| Path                    | Persistence implementation                                           |
| ----------------------- | -------------------------------------------------------------------- |
| `/data/attachments`     | Re-configured to `/mnt/s3/attachments`.                              |
| `/data/icon_cache`      | Re-configured to `/mnt/s3/icon_cache`.                               |
| `/data/sends`           | Re-configured to `/mnt/s3/sends`.                                    |
| `/data/config.json`     | Auto-generated on startup from environment variables.                |
| `/data/db.sqlite`       | Replicated to S3 via [Litestream] to `vaulwarden.db/` in the bucket. |
| `/data/rsa_key.pem`     | Initialized from `VAULTWARDEN_RSA_PRIVATE_KEY` environment variable. |
| `/data/rsa_key.pub.pem` | Initialized from `VAULTWARDEN_RSA_PRIVATE_KEY` environment variable. |

The `/mnt/s3` directory is a [GeeseFS] mount to the `data/` path in the same S3 bucket that the SQlite database
is backed up to.

> Note that because we generate the `config.json` from environment variables, modifying it in the Admin UI or
> organization settings will not work. These settings must be changed from environment variables in your `fly.toml`
> or via `fly secrets set`.

### Environment variables

**S3 configuration**

| Variable                | Default       | Description |
| ----------------------- | ------------- | ----------- |
| `AWS_ACCESS_KEY_ID`     | n/a, required |             |
| `AWS_SECRET_ACCESS_KEY` | n/a, required |             |
| `AWS_REGION`            | n/a, required |             |
| `AWS_ENDPOINT_URL_S3`   | n/a, required |             |
| `BUCKET_NAME`           | n/a, required |             |

**Secrets**

**Vaultwarden configuration**

| Variable                                  | Default                            | Description                                                                                                                                                             |
| ----------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VAULTWARDEN_ADMIN_TOKEN`                 | n/a, required                      | Token to enter the Vaultwarden admin panel with. Create it with `docker run -it --rm ghcr.io/dani-garcia/vaultwarden /vaultwarden hash`, may be stored as a non-secret. |
| `VAULTWARDEN_RSA_PRIVATE_KEY`             | n/a, required                      | The RSA 2048-bits private key that Vaultwarden uses to sign JWTs. Generate with `openssl genrsa 2048`. If you change this value, all current JWTs are invalidated.      |
| `VAULTWARDEN_LOG_LEVEL`                   | `info`                             |                                                                                                                                                                         |
| `VAULTWARDEN_DOMAIN`                      | `https://${FLY_APP_NAME}.fly.dev`  | The public URL of your Vaultwarden deployment.                                                                                                                          |
| `VAULTWARDEN_IP_HEADER`                   | `X-Real-IP`                        | The HTTP header used to determine the client's real IP address. Set to `CF-Connecting-IP` when using Cloudflare in front of Fly.io.                                     |
| `VAULTWARDEN_SENDS_ALLOWED`               | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_HIBP_API_KEY`                | (empty string)                     | Have I been Pwnd! API Key                                                                                                                                               |
| `VAULTWARDEN_SIGNUPS_ALLOWED`             | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_SIGNUPS_VERIFY`              | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME`  | `3600`                             |                                                                                                                                                                         |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT` | `6`                                |                                                                                                                                                                         |
| `VAULTWARDEN_INVITATIONS_ALLOWED`         | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED`    | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_EMAIL_CHANGE_ALLOWED`        | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_PASSWORD_ITERATIONS`         | `600000`                           |                                                                                                                                                                         |
| `VAULTWARDEN_PASSWORD_HINTS_ALLOWED`      | `true`                             |                                                                                                                                                                         |
| `VAULTWARDEN_PUSH_INSTALLATION_ID`        | n/a                                | Obtain your installation ID and key here to enable push notifications: https://bitwarden.com/host/                                                                      |
| `VAULTWARDEN_PUSH_INSTALLATION_KEY`       | n/a                                | Must be set if `VAULTWARDEN_PUSH_INSTALLATION_ID` is set.                                                                                                               |
| `VAULTWARDEN_SHOW_PASSWORD_HINT`          | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_INVITATION_ORG_NAME`         | `Vaultwarden`                      |                                                                                                                                                                         |
| `VAULTWARDEN_DISABLE_2FA_REMEMBER`        | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_USE_SENDMAIL`                | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_ENABLE_DUO`                  | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_ENABLE_SMTP`                 | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_ENABLE_EMAIL_2FA`            | value of `VAULTWARDEN_ENABLE_SMTP` |                                                                                                                                                                         |
| `VAULTWARDEN_ENABLE_SMTP`                 | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_HOST`                   | n/a, required if SMTP enabled      |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_SECURITY`               | force_tls                          |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_PORT`                   | 465                                |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_FROM`                   | n/a, required if SMTP enabled      |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_FROM_NAME`              | Vaultwarden                        |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_USERNAME`               | n/a, required if SMTP enabled      |                                                                                                                                                                         |
| `VAULTWARDEN_SMTP_PASSWORD`               | n/a, required if SMTP enabled      |                                                                                                                                                                         |
| `VAULTWARDEN_ENABLE_YUBICO`               | `false`                            |                                                                                                                                                                         |
| `VAULTWARDEN_YUBICO_CLIENT_ID`            | n/a, required if Yubico enabled    |                                                                                                                                                                         |
| `VAULTWARDEN_YUBICO_SECRET_KEY`           | n/a, required if Yubico enabled    |                                                                                                                                                                         |

**GeeseFS variables**

| Variable               | Default | Description                                                                                                                              |
| ---------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `GEESEFS_ENABLED`      | `true`  | If set to `false`, GeeseFS will not be used and related data directories will _not_ be mounted. Use with care, this is for testing only. |
| `GEESEFS_MEMORY_LIMIT` | `64`    | The memory limit in MB for GeeseFS.                                                                                                      |
| `GEESEFS_MONITOR_ENABLED`           | `true` | Periodically check that `/mnt/s3` still works and restart the machine if it does not (see below). |
| `GEESEFS_MONITOR_INTERVAL`          | `30`   | Seconds between checks.                                                                           |
| `GEESEFS_MONITOR_TIMEOUT`           | `20`   | Seconds after which listing `/mnt/s3` is considered hung.                                        |
| `GEESEFS_MONITOR_FAILURE_THRESHOLD` | `3`    | Number of consecutive failed checks before the mount is considered unrecoverable.                |
| `GEESEFS_MONITOR_WRITE_CHECK`       | `true` | Also write, fsync and read back a small file (`/mnt/s3/.s3-monitor-<machine id>`) on every check. Each check then makes one S3 upload; set to `false` to only list the directory. |

The S3 monitor runs alongside Vaultwarden. If the GeeseFS process dies or `/mnt/s3` is no longer mounted, or if listing
`/mnt/s3` (or writing the probe file) fails or hangs for `GEESEFS_MONITOR_FAILURE_THRESHOLD` checks in a row, it logs an
error, sends `SIGTERM` to Litestream/Vaultwarden (followed by `SIGKILL` after 30 seconds) and the container exits with
status `1`. Fly.io then restarts the machine according to its restart policy (`on-failure` by default), which mounts the
bucket again.

Before restarting, the monitor checks that the bucket itself is reachable with `mc`. If it is not (e.g. an outage of the
S3 provider), the machine is _not_ restarted, since its disk does not survive a restart and Litestream could neither
upload its pending changes nor restore the database. Vaultwarden keeps serving from the local database, and the restart
happens once S3 is reachable again and the mount is still broken.

**Litestream variables**

| Variable                              | Default       | Description                                                                                                                                                                                     |
| ------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGE_SECRET_KEY`                      | n/a, required |                                                                                                                                                                                                 |
| `LITESTREAM_ENABLED`                  | `true`        | Whether to restore and replicate the SQlite database with Litestream. You likely never want to turn this option off, as you will loose your SQlite database on restarts.                        |
| `LITESTREAM_RETENTION`                | `24h`         | Configure the Litestream retention period. Retention is enforced periodically and can be changed with `LITESTREAM_RETENTION_CHECK_INTERVAL`.                                                    |
| `LITESTREAM_RETENTION_CHECK_INTERVAL` | `1h`          | The interval at which retention should be applied.                                                                                                                                              |
| `LITESTREAM_VALIDATION_INTERVAL`      | `12h`         | The interval at which Litestream does a separate restore of the database and validates the result vs. the current database.                                                                     |
| `LITESTREAM_SYNC_INTERVAL`            | `10s`         | Frequency in which frames are pushed to the replica. Note that Litestream's typical default is `1s`, and increasing this frequency can increase storage costs due to higher API request counts. |

**Maintenance variables**

| Variable          | Default | Description                                                                                                                                                                                                                                                                      |
| ----------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENTRYPOINT_IDLE` | `false` | If set to `true`, enter idle mode before launching the application or if an error occurs on startup. Note that Fly.io might stop the machine after a short while.                                                                                                                |
| `IMPORT_DATABASE` | `false` | If set to `true`, the startup process will check for an `import-db.sqlite` file in the S3 bucket and load that instead of `litestream restore`. Use for migrating from another Vaultwarden instead. Should be turned off immediately after the litestream replication succeeded. |

**Scheduled recovery backups**

The optional worker captures the local SQLite database with its online backup API, downloads attachments and sends
from S3, and includes recovery configuration and credentials. It uploads an Age-encrypted archive to a separate S3
destination. Vaultwarden keeps running during capture. See [backup operation and recovery](docs/backups.md) for
the archive format, consistency limits, destination permissions, and restore steps.

| Variable | Default | Description |
| --- | --- | --- |
| `BACKUP_ENABLED` | `false` | Enable the supervised backup worker. Requires GeeseFS/S3 file storage. |
| `BACKUP_BUCKET_NAME` | Required when enabled | Destination bucket, independent of `BUCKET_NAME`. |
| `BACKUP_PREFIX` | Required when enabled | Application-specific prefix, e.g. `kalix.cluster.rosenstein.app/vaultwarden/`. |
| `BACKUP_AWS_ACCESS_KEY_ID` | Required when enabled | Destination access key; source credentials are never used as a fallback. |
| `BACKUP_AWS_SECRET_ACCESS_KEY` | Required when enabled | Destination secret key. |
| `BACKUP_AWS_SESSION_TOKEN` | Unset | Session token when using temporary destination credentials. Must be refreshed externally. |
| `BACKUP_AWS_REGION` | Required when enabled | Destination signing region. |
| `BACKUP_AWS_ENDPOINT_URL_S3` | AWS regional endpoint | Optional endpoint for an S3-compatible destination supporting conditional PUT and SHA-256 checksums. |
| `BACKUP_AGE_RECIPIENT` | Required when enabled | Recovery public key. Keep its private key outside this deployment and Vaultwarden, separate from the Litestream Age identity. |
| `BACKUP_INTERVAL_SECONDS` | `3600` | Time between published capture start timestamps (including degraded backups), persisted through S3 completion manifests. |
| `BACKUP_TIMEOUT_SECONDS` | `1800` | Maximum time per scheduling check/capture/upload attempt. |
| `BACKUP_MAX_BYTES` | `1073741824` | Maximum captured payload bytes; at most 4 GiB. Archives use a single S3 PUT. |
| `BACKUP_TMP_DIR` | System temporary directory | Private staging directory parent. Provision at least twice the payload size in free disk space. |

On startup, the worker reads the latest usable completion record from S3 and waits until its next due time. It
skips invalid records and captures immediately if no usable record exists or the interval has elapsed. Missing
referenced files produce a degraded archive with recovery details; both complete and degraded archives advance
the schedule. Monitor freshness and degraded status separately. Failed checks/captures retry with backoff
without restarting Vaultwarden; an unavailable destination is never treated as an empty backup history. Only one
capture runs at a time. Keep the application running to meet the interval: a stopped Fly Machine cannot run backups.

Recovery fields are explicitly listed in `vaultwarden-fly-io/backup.py`. Every environment option documented in
this README must be captured or have a named exclusion with a reason; CI checks this contract. Backup-destination
options and one-time maintenance flags are excluded. The worker does not export unrelated environment variables
or access a secrets manager.


### Development checks

Install uv and `age`, then run from the repository root (uv manages Python 3.12):

```sh
uv sync --locked --group dev --python 3.12
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync python -m mypy
uv run --no-sync python -m unittest discover -s tests -v
```

CI requires lint, formatting, strict type checks, and tests before building the image.
Use `uv run --no-sync ruff format .` to apply formatting. Dependencies are declared in `pyproject.toml`
and locked in `uv.lock`; run `uv lock` after editing them, or `uv lock --upgrade` to update resolved versions.
SDK stubs and checking tools belong to the `dev` group. The image installs runtime dependencies through Alpine.
