# Vaultwarden on Fly.io

Run [Vaultwarden] on [Fly.io] with reliable [Litestream] SQlite replication and attachments/sends stored in S3.

Using the smallest VM size on Fly.io (`shared-cpu-1x`) and leveraging the Tigris object storage free tier, this
costs approx. 2 USD/mo to run (depending on the region). Small Vaultwarden instances won't see traffic 24/7, so
you should pay much less because your VM can be stopped for a large portion of the time.

  [Vaultwarden]: https://github.com/dani-garcia/vaultwarden
  [Fly.io]: https://fly.io/
  [Litestream]: https://litestream.io/

## Prerequisites

* An account on [Fly.io]
* The [fly](https://github.com/superfly/flyctl) CLI
* The [age](https://github.com/FiloSottile/age) CLI

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
|-------------------------|----------------------------------------------------------------------|
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

__S3 configuration__

| Variable                | Default       | Description |
|-------------------------|---------------|-------------|
| `AWS_ACCESS_KEY_ID`     | n/a, required |             |
| `AWS_SECRET_ACCESS_KEY` | n/a, required |             |
| `AWS_REGION`            | n/a, required |             |
| `AWS_ENDPOINT_URL_S3`   | n/a, required |             |
| `BUCKET_NAME`           | n/a, required |             |

__Secrets__


__Vaultwarden configuration__

| Variable                                  | Default                            | Description                                                                                                                                                             |
|-------------------------------------------|------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `VAULTWARDEN_ADMIN_TOKEN`                 | n/a, required                      | Token to enter the Vaultwarden admin panel with. Create it with `docker run -it --rm ghcr.io/dani-garcia/vaultwarden /vaultwarden hash`, may be stored as a non-secret. |
| `VAULTWARDEN_RSA_PRIVATE_KEY`             | n/a, required                      | The RSA 2048-bits private key that Vaultwarden uses to sign JWTs. Generate with `openssl genrsa 2048`. If you change this value, all current JWTs are invalidated.      |
| `VAULTWARDEN_LOG_LEVEL`                   | `info`                             |                                                                                                                                                                         |
| `VAULTWARDEN_DOMAIN`                      | `https://${FLY_APP_NAME}.fly.dev`  | The public URL of your Vaultwarden deployment.                                                                                                                          |
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

__GeeseFS variables__

| Variable               | Default | Description                                                                                                                              |
|------------------------|---------|------------------------------------------------------------------------------------------------------------------------------------------|
| `GEESEFS_ENABLED`      | `true`  | If set to `false`, GeeseFS will not be used and related data directories will _not_ be mounted. Use with care, this is for testing only. |
| `GEESEFS_MEMORY_LIMIT` | `64`    | The memory limit in MB for GeeseFS.                                                                                                      |

__Litestream variables__

| Variable                              | Default       | Description |
|---------------------------------------|---------------|-------------|
| `AGE_SECRET_KEY`                      | n/a, required |             |
| `LITESTREAM_RETENTION`                | `24h`         |             |
| `LITESTREAM_RETENTION_CHECK_INTERVAL` | `1h`          |             |
| `LITESTREAM_VALIDATION_INTERVAL`      | `12h`         |             |

__Maintenance variables__

| Variable          | Default | Description                                                                                                                                                                                                                                                                      |
|-------------------|---------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ENTRYPOINT_IDLE` | `false` | If set to `true`, enter idle mode before launching the application or if an error occurs on startup. Note that Fly.io might stop the machine after a short while.                                                                                                                |
| `IMPORT_DATABASE` | `false` | If set to `true`, the startup process will check for an `import-db.sqlite` file in the S3 bucket and load that instead of `litestream restore`. Use for migrating from another Vaultwarden instead. Should be turned off immediately after the litestream replication succeeded. |
