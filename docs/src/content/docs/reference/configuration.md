---
title: Configuration
description: Every environment variable understood by the vaultwarden-fly-io image.
tableOfContents:
  maxHeadingLevel: 2
---

The image is configured entirely through environment variables. Set non-sensitive values in the `[env]` section of
`fly.toml` and sensitive ones with `fly secrets set`.

:::note
Vaultwarden's `config.json` is regenerated from these variables on every start and made read-only. Changes made in
the admin panel don't persist. Change the variables instead.
:::

## S3 storage

`fly storage create` sets these as secrets for you. With an AWS S3 bucket, you can use an IAM role instead of access
keys, see [AWS S3 without access keys](../../guides/aws-oidc/).

| Variable | Default | Description |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | required without a role | Access key for the application bucket. Takes precedence over `AWS_ROLE_ARN`. |
| `AWS_SECRET_ACCESS_KEY` | required without a role | Secret key for the application bucket. |
| `AWS_ROLE_ARN` | unset | IAM role to assume with the OIDC token in `AWS_WEB_IDENTITY_TOKEN_FILE`. AWS S3 only. |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | set by Fly.io and EKS | File containing the OIDC token for `AWS_ROLE_ARN`. The entrypoint waits up to 30 seconds for it to appear. |
| `AWS_ROLE_SESSION_NAME` | set by Fly.io | Role session name, visible in CloudTrail. |
| `AWS_REGION` | required | Signing region of the bucket. |
| `AWS_ENDPOINT_URL_S3` | AWS S3 in `AWS_REGION` | S3 endpoint URL. Required for any provider other than AWS. |
| `BUCKET_NAME` | required | Bucket holding the Litestream replica (`vaultwarden.db/`) and files (`data/`). |

## Vaultwarden

Each variable maps to the [Vaultwarden option](https://github.com/dani-garcia/vaultwarden/blob/main/.env.template) of
the same name, without the `VAULTWARDEN_` prefix.

### Core

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_ADMIN_TOKEN` | required | Admin panel token. Hash it with `docker run -it --rm ghcr.io/dani-garcia/vaultwarden /vaultwarden hash`. Because it's hashed, it can be a plain environment variable. |
| `VAULTWARDEN_RSA_PRIVATE_KEY` | required | 2048-bit RSA private key used to sign JWTs. Generate it with `openssl genrsa 2048`. Changing it invalidates all current sessions. |
| `VAULTWARDEN_DOMAIN` | `https://${FLY_APP_NAME}.fly.dev` | Public URL of your deployment. |
| `VAULTWARDEN_LOG_LEVEL` | `info` | Log verbosity. |
| `VAULTWARDEN_IP_HEADER` | `X-Real-IP` | Header containing the client's real IP. Set it to `CF-Connecting-IP` when Cloudflare is in front of Fly.io. |

### Accounts and sign-ups

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_SIGNUPS_ALLOWED` | `true` | Allow anyone to register. Set it to `false` once your accounts exist. |
| `VAULTWARDEN_SIGNUPS_VERIFY` | `false` | Require email verification before the first login. Needs SMTP. |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME` | `3600` | Seconds between verification email resends. |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT` | `6` | Maximum number of verification email resends. |
| `VAULTWARDEN_INVITATIONS_ALLOWED` | `true` | Allow organization admins to invite users, even when sign-ups are disabled. |
| `VAULTWARDEN_INVITATION_ORG_NAME` | `Vaultwarden` | Name used in invitation emails. |
| `VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED` | `true` | Enable the emergency access feature. |
| `VAULTWARDEN_EMAIL_CHANGE_ALLOWED` | `true` | Allow users to change their email address. |
| `VAULTWARDEN_PASSWORD_ITERATIONS` | `600000` | Server-side password hashing iterations. |
| `VAULTWARDEN_PASSWORD_HINTS_ALLOWED` | `true` | Allow users to set password hints. |
| `VAULTWARDEN_SHOW_PASSWORD_HINT` | `false` | Show hints on the login page instead of sending them by email. |
| `VAULTWARDEN_SENDS_ALLOWED` | `true` | Enable Bitwarden Send. |
| `VAULTWARDEN_HIBP_API_KEY` | empty | [Have I Been Pwned](https://haveibeenpwned.com/API/Key) API key for breach reports. |

### Two-factor authentication

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_DISABLE_2FA_REMEMBER` | `false` | Disable "remember me" for two-factor logins. |
| `VAULTWARDEN_ENABLE_EMAIL_2FA` | value of `VAULTWARDEN_ENABLE_SMTP` | Enable email as a second factor. |
| `VAULTWARDEN_ENABLE_DUO` | `false` | Enable Duo as a second factor. |
| `VAULTWARDEN_ENABLE_YUBICO` | `false` | Enable YubiKey OTP as a second factor. |
| `VAULTWARDEN_YUBICO_CLIENT_ID` | required if Yubico is enabled | Yubico API client ID. |
| `VAULTWARDEN_YUBICO_SECRET_KEY` | required if Yubico is enabled | Yubico API secret key. |

### Email

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_ENABLE_SMTP` | `false` | Send email through SMTP. |
| `VAULTWARDEN_SMTP_HOST` | required if SMTP is enabled | SMTP server hostname. |
| `VAULTWARDEN_SMTP_SECURITY` | `force_tls` | `starttls`, `force_tls` or `off`. |
| `VAULTWARDEN_SMTP_PORT` | `465` | SMTP server port. |
| `VAULTWARDEN_SMTP_FROM` | required if SMTP is enabled | Sender address. |
| `VAULTWARDEN_SMTP_FROM_NAME` | `Vaultwarden` | Sender display name. |
| `VAULTWARDEN_SMTP_USERNAME` | required if SMTP is enabled | SMTP username. |
| `VAULTWARDEN_SMTP_PASSWORD` | required if SMTP is enabled | SMTP password. |
| `VAULTWARDEN_USE_SENDMAIL` | `false` | Use `sendmail` instead of SMTP. |

### Push notifications

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_PUSH_INSTALLATION_ID` | unset | Installation ID from [bitwarden.com/host](https://bitwarden.com/host/). Enables push notifications to mobile clients. |
| `VAULTWARDEN_PUSH_INSTALLATION_KEY` | required if the ID is set | Installation key from the same page. |

## Litestream

| Variable | Default | Description |
| --- | --- | --- |
| `AGE_SECRET_KEY` | required | age identity that encrypts the replica. Keep a copy outside Fly.io. |
| `LITESTREAM_ENABLED` | `true` | Restore and replicate the database with Litestream. If you turn this off, the database is lost on every restart. |
| `LITESTREAM_SYNC_INTERVAL` | `10s` | How often changes are pushed to the replica. Litestream's own default is `1s`. This image uses `10s` to cap the number of S3 requests, see the [cost model](../costs/#litestream-sync-interval). Lower it to shrink the window of changes lost on a crash. |
| `LITESTREAM_RETENTION` | `24h` | How long snapshots and WAL segments are kept. |
| `LITESTREAM_RETENTION_CHECK_INTERVAL` | `1h` | How often retention is enforced. |
| `LITESTREAM_VALIDATION_INTERVAL` | `12h` | How often Litestream restores the replica separately and compares it with the live database. |

## GeeseFS

| Variable | Default | Description |
| --- | --- | --- |
| `GEESEFS_ENABLED` | `true` | Mount the bucket's `data/` prefix at `/mnt/s3`. If `false`, attachments, Sends and icons aren't persisted. For testing only. |
| `GEESEFS_MEMORY_LIMIT` | `64` | GeeseFS memory limit in MB. |

### S3 mount monitor

| Variable | Default | Description |
| --- | --- | --- |
| `GEESEFS_MONITOR_ENABLED` | `true` | Restart the machine when `/mnt/s3` stops working. See [how the monitor works](../architecture/#s3-mount-monitor). |
| `GEESEFS_MONITOR_INTERVAL` | `30` | Seconds between checks. |
| `GEESEFS_MONITOR_TIMEOUT` | `20` | Seconds after which a check is considered hung. |
| `GEESEFS_MONITOR_FAILURE_THRESHOLD` | `3` | Consecutive failed checks before the mount is considered broken. |
| `GEESEFS_MONITOR_WRITE_CHECK` | `true` | Also write and fsync `/mnt/s3/.s3-monitor-<machine id>` on every check. That is one S3 upload per check, 86,400 a month at the default interval (see the [cost model](../costs/)). Set it to `false` to only list the directory. |

## Scheduled backups

See the [backups guide](../../guides/backups/) for how these fit together.

| Variable | Default | Description |
| --- | --- | --- |
| `BACKUP_ENABLED` | `false` | Run the backup worker. Requires GeeseFS. |
| `BACKUP_BUCKET_NAME` | required when enabled | Destination bucket, separate from `BUCKET_NAME`. |
| `BACKUP_PREFIX` | required when enabled | Application-specific key prefix, for example `vaultwarden/`. |
| `BACKUP_AWS_ACCESS_KEY_ID` | required without a role | Destination access key. The source credentials are never used as a fallback. |
| `BACKUP_AWS_SECRET_ACCESS_KEY` | required without a role | Destination secret key. |
| `BACKUP_AWS_SESSION_TOKEN` | unset | Session token for temporary destination credentials. You must refresh it yourself. |
| `BACKUP_AWS_ROLE_ARN` | unset | IAM role to assume with an OIDC token instead of using access keys (see [AWS S3 without access keys](../../guides/aws-oidc/)). Can't be combined with the access keys above. The application's `AWS_ROLE_ARN` is never used. |
| `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` | Fly.io machine API | File containing the OIDC token (JWT). It is read again on every refresh, so it may be rotated in place. On Fly.io, leave it unset to request tokens from the machine API instead. Required with a role elsewhere. The application's `AWS_WEB_IDENTITY_TOKEN_FILE` is never used. |
| `BACKUP_AWS_ROLE_SESSION_NAME` | `vaultwarden-backup` | Role session name, visible in CloudTrail. |
| `BACKUP_AWS_REGION` | required when enabled | Destination signing region. With a role, also the region of the STS endpoint. |
| `BACKUP_AWS_ENDPOINT_URL_S3` | AWS regional endpoint | Endpoint of an S3-compatible destination that supports conditional PUT and SHA-256 checksums. |
| `BACKUP_AGE_RECIPIENT` | required when enabled | Public key for recovery. Keep the private key outside this deployment, separate from `AGE_SECRET_KEY`. |
| `BACKUP_INTERVAL_SECONDS` | `3600` | Time between capture start times, including degraded backups. The schedule survives restarts through the completion manifests in S3. |
| `BACKUP_TIMEOUT_SECONDS` | `1800` | Maximum duration of each scheduling check or capture/upload attempt. |
| `BACKUP_MAX_BYTES` | `1073741824` | Maximum captured payload in bytes, at most 4 GiB. |
| `BACKUP_TMP_DIR` | system temp directory | Parent directory for private staging. Needs free space of at least twice the payload size. |

## Maintenance

| Variable | Default | Description |
| --- | --- | --- |
| `IMPORT_DATABASE` | unset | Key of an SQLite database in the bucket, for example `import-db.sqlite`. If set, it is loaded instead of running `litestream restore`. Used for [migrations](../../guides/migration/). Unset it as soon as replication has succeeded. |
| `ENTRYPOINT_IDLE` | `false` | Idle before starting the application, or when startup fails, so you can `fly ssh console` in to debug. Fly.io may stop the machine after a while. |
