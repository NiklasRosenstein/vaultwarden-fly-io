---
title: Settings
description: Every environment variable the image understands.
tableOfContents:
  maxHeadingLevel: 2
---

All settings are environment variables. Put normal values in the `[env]` section of `fly.toml`. Put passwords and
keys in secrets with `fly secrets set`.

:::note
Settings changed in the admin panel are lost on restart. Change the variables instead.
:::

## Most common

These are the settings most people change. Each one is also listed in its section below.

| Variable | Why you would change it |
| --- | --- |
| `VAULTWARDEN_DOMAIN` | You use your own domain. |
| `VAULTWARDEN_ADMIN_TOKEN` | Password for the admin panel. Without it, the admin panel is off. |
| `VAULTWARDEN_SIGNUPS_ALLOWED` | Set to `false` once your accounts exist. |
| `VAULTWARDEN_ENABLE_SMTP` | You want invitation and verification emails. See [Email](#email). |
| `VAULTWARDEN_IP_HEADER` | You use Cloudflare or another proxy. |

## S3 storage

`fly storage create` sets these for you. On AWS S3 you can use a role instead of keys, see
[AWS S3 without access keys](../../other-setups/aws-s3-without-access-keys/).

| Variable | Default | Description |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | required without a role | Access key. Wins over `AWS_ROLE_ARN` if both are set. |
| `AWS_SECRET_ACCESS_KEY` | required without a role | Secret key. |
| `AWS_ROLE_ARN` | unset | IAM role to use instead of keys. AWS S3 only. |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | set by Fly.io and EKS | File with the token for `AWS_ROLE_ARN`. |
| `AWS_ROLE_SESSION_NAME` | set by Fly.io | Name shown in CloudTrail. |
| `AWS_REGION` | required | Region of the bucket. |
| `AWS_ENDPOINT_URL_S3` | AWS S3 in `AWS_REGION` | S3 address. Required for providers other than AWS. |
| `BUCKET_NAME` | required | Bucket for the database and files. |

## Vaultwarden

Each variable is the [Vaultwarden option](https://github.com/dani-garcia/vaultwarden/blob/main/.env.template) of the
same name, with `VAULTWARDEN_` in front.

### Core

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_ADMIN_TOKEN` | empty | Admin panel password, hashed. Create it with `docker run -it --rm ghcr.io/dani-garcia/vaultwarden /vaultwarden hash`. Leave it unset or empty to turn the admin panel off. |
| `VAULTWARDEN_RSA_PRIVATE_KEY` | required | Key that signs logins. Create it with `openssl genrsa 2048`. Changing it logs everyone out. |
| `VAULTWARDEN_DOMAIN` | `https://${FLY_APP_NAME}.fly.dev` | Public address of your vault. |
| `VAULTWARDEN_LOG_LEVEL` | `info` | How much to log. |
| `VAULTWARDEN_IP_HEADER` | `X-Real-IP` | Header with the visitor's IP address. Use `CF-Connecting-IP` behind Cloudflare. |

### Accounts and sign-ups

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_SIGNUPS_ALLOWED` | `true` | Let anyone create an account. |
| `VAULTWARDEN_SIGNUPS_VERIFY` | `false` | Require email verification before the first login. Needs email. |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME` | `3600` | Seconds between verification emails. |
| `VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT` | `6` | Maximum number of verification emails. |
| `VAULTWARDEN_INVITATIONS_ALLOWED` | `true` | Let organization admins invite users, even with sign-ups off. |
| `VAULTWARDEN_INVITATION_ORG_NAME` | `Vaultwarden` | Name used in invitation emails. |
| `VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED` | `true` | Turn on emergency access. |
| `VAULTWARDEN_EMAIL_CHANGE_ALLOWED` | `true` | Let users change their email address. |
| `VAULTWARDEN_PASSWORD_ITERATIONS` | `600000` | Password hashing rounds on the server. |
| `VAULTWARDEN_PASSWORD_HINTS_ALLOWED` | `true` | Let users set password hints. |
| `VAULTWARDEN_SHOW_PASSWORD_HINT` | `false` | Show hints on the login page instead of sending them by email. |
| `VAULTWARDEN_SENDS_ALLOWED` | `true` | Turn on Bitwarden Send. |
| `VAULTWARDEN_HIBP_API_KEY` | empty | [Have I Been Pwned](https://haveibeenpwned.com/API/Key) key for breach reports. |

### Two-factor login

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_DISABLE_2FA_REMEMBER` | `false` | Turn off "remember me" for two-factor logins. |
| `VAULTWARDEN_ENABLE_EMAIL_2FA` | value of `VAULTWARDEN_ENABLE_SMTP` | Allow email codes as a second factor. |
| `VAULTWARDEN_EMAIL_2FA_ENFORCE_ON_VERIFIED_INVITE` | `false` | Turn on email two-factor login for users who accept an invitation and have no other second factor. |
| `VAULTWARDEN_EMAIL_2FA_AUTO_FALLBACK` | `false` | Turn on email two-factor login for users whose last other second factor is removed. |
| `VAULTWARDEN_ENABLE_DUO` | `false` | Allow Duo as a second factor. |
| `VAULTWARDEN_ENABLE_YUBICO` | `false` | Allow YubiKey OTP as a second factor. |
| `VAULTWARDEN_YUBICO_CLIENT_ID` | required for Yubico | Yubico client ID. |
| `VAULTWARDEN_YUBICO_SECRET_KEY` | required for Yubico | Yubico secret key. |

### Email

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_ENABLE_SMTP` | `false` | Send email through an SMTP server. |
| `VAULTWARDEN_SMTP_HOST` | required for SMTP | Server name. |
| `VAULTWARDEN_SMTP_SECURITY` | `force_tls` | `starttls`, `force_tls` or `off`. |
| `VAULTWARDEN_SMTP_PORT` | `465` | Server port. |
| `VAULTWARDEN_SMTP_FROM` | required for SMTP | Sender address. |
| `VAULTWARDEN_SMTP_FROM_NAME` | `Vaultwarden` | Sender name. |
| `VAULTWARDEN_SMTP_USERNAME` | required for SMTP | User name. |
| `VAULTWARDEN_SMTP_PASSWORD` | required for SMTP | Password. |
| `VAULTWARDEN_USE_SENDMAIL` | `false` | Use `sendmail` instead of SMTP. |

### Push notifications

| Variable | Default | Description |
| --- | --- | --- |
| `VAULTWARDEN_PUSH_INSTALLATION_ID` | unset | ID from [bitwarden.com/host](https://bitwarden.com/host/). Turns on push notifications for phones. |
| `VAULTWARDEN_PUSH_INSTALLATION_KEY` | required with the ID | Key from the same page. |

## Database copy (Litestream)

| Variable | Default | Description |
| --- | --- | --- |
| `AGE_SECRET_KEY` | required | Key that encrypts the database copy. Keep a copy outside Fly.io. |
| `LITESTREAM_ENABLED` | `true` | Copy the database to S3. If `false`, the database is lost on every restart. Older images ignored this setting, so make sure it isn't set before you upgrade. |
| `LITESTREAM_SYNC_INTERVAL` | `10s` | How often changes are uploaded. Shorter means less loss on a crash, but more cost. See [Costs](../costs/#litestream-sync-interval). |
| `LITESTREAM_RETENTION` | `24h` | How long old copies are kept. |
| `LITESTREAM_RETENTION_CHECK_INTERVAL` | `1h` | How often old copies are removed. |
| `LITESTREAM_VALIDATION_INTERVAL` | `12h` | How often the copy is checked against the live database. |

## Files in S3 (GeeseFS)

| Variable | Default | Description |
| --- | --- | --- |
| `GEESEFS_ENABLED` | `true` | Store attachments, Sends and icons in the bucket. If `false`, they are lost on restart. For testing only. |
| `GEESEFS_MEMORY_LIMIT` | `64` | Memory limit in MB. |

### S3 mount monitor

See [how the monitor works](../how-it-works/#s3-mount-monitor).

| Variable | Default | Description |
| --- | --- | --- |
| `GEESEFS_MONITOR_ENABLED` | `true` | Restart the machine when the bucket folder stops working. |
| `GEESEFS_MONITOR_INTERVAL` | `30` | Seconds between checks. |
| `GEESEFS_MONITOR_TIMEOUT` | `20` | Seconds before a check counts as failed. |
| `GEESEFS_MONITOR_FAILURE_THRESHOLD` | `3` | Failed checks in a row before a restart. |
| `GEESEFS_MONITOR_WRITE_CHECK` | `true` | Also write a small test file on each check. This is the biggest source of S3 requests, see [Costs](../costs/). |

## Scheduled backups

See [Turn on backups](../../keep-your-data-safe/turn-on-backups/).

| Variable | Default | Description |
| --- | --- | --- |
| `BACKUP_ENABLED` | `false` | Turn on backups. Needs GeeseFS. |
| `BACKUP_BUCKET_NAME` | required when on | Backup bucket. Must differ from `BUCKET_NAME`. |
| `BACKUP_PREFIX` | required when on | Folder in the bucket, for example `vaultwarden/`. |
| `BACKUP_AWS_ACCESS_KEY_ID` | required without a role | Access key for the backup bucket. The app's keys are never used. |
| `BACKUP_AWS_SECRET_ACCESS_KEY` | required without a role | Secret key for the backup bucket. |
| `BACKUP_AWS_SESSION_TOKEN` | unset | Session token for short-lived keys. You must renew it yourself. |
| `BACKUP_AWS_ROLE_ARN` | unset | IAM role to use instead of keys, see [AWS S3 without access keys](../../other-setups/aws-s3-without-access-keys/). Can't be used together with keys. |
| `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` | Fly.io machine API | File with the token for the role. Required outside Fly.io. On Fly.io, leave it unset: the app gets tokens from the machine's local Fly.io API. |
| `BACKUP_AWS_ROLE_SESSION_NAME` | `vaultwarden-backup` | Name shown in CloudTrail. |
| `BACKUP_AWS_REGION` | required when on | Region of the backup bucket. |
| `BACKUP_AWS_ENDPOINT_URL_S3` | AWS endpoint of the region | S3 address for other providers. The provider must support conditional uploads and SHA-256 checksums. |
| `BACKUP_AGE_RECIPIENT` | required when on | Public recovery key (`age1...`). Keep the private key away from the app. |
| `BACKUP_INTERVAL_SECONDS` | `3600` | Seconds between backups. |
| `BACKUP_TIMEOUT_SECONDS` | `1800` | Maximum time for one backup. |
| `BACKUP_MAX_BYTES` | `1073741824` | Maximum backup size in bytes. At most 4 GiB. |
| `BACKUP_TMP_DIR` | system temp folder | Where backups are prepared. Needs free space of twice the backup size. |

## Maintenance

| Variable | Default | Description |
| --- | --- | --- |
| `IMPORT_DATABASE` | unset | Bucket key of a database to load on start, for example `import-db.sqlite`. See [Move from another server](../../get-started/move-from-another-server/). Remove it right after the import. |
| `ENTRYPOINT_IDLE` | `false` | Wait instead of starting, or instead of exiting on an error, so you can log in with `fly ssh console`. |
