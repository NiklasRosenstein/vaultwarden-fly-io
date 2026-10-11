---
title: AWS S3 without access keys
description: Reach the application and backup buckets with temporary credentials from OIDC instead of long-lived access keys.
---

Normally the app uses fixed access keys to reach its buckets. With AWS S3 you can avoid stored keys. Fly.io and
Kubernetes give each machine or pod a signed identity token (an OIDC token). AWS trades that token for short-lived
keys through an IAM role. The app renews them before they expire.

You can do this for the app bucket (`AWS_*` variables), the [backup](../backups/) bucket (`BACKUP_AWS_*`), or both.

:::note
This only works with AWS S3, not with Tigris or other providers.
:::

## Trust the token issuer

Create one IAM role per bucket. Use separate roles, so the app can't delete its own backups. Then tell AWS to trust
tokens from your platform.

### On Fly.io

See Fly.io's [OpenID Connect](https://fly.io/docs/security/openid-connect/) docs.

1. In AWS IAM, add an OpenID Connect identity provider. URL: `https://oidc.fly.io/<org-slug>`. Audience:
   `sts.amazonaws.com`.
2. Give the role this trust policy. It only accepts tokens from your app:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "Federated": "arn:aws:iam::<account>:oidc-provider/oidc.fly.io/<org-slug>" },
         "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {
           "StringEquals": { "oidc.fly.io/<org-slug>:aud": "sts.amazonaws.com" },
           "StringLike": { "oidc.fly.io/<org-slug>:sub": "<org-slug>:<app-name>:*" }
         }
       }
     ]
   }
   ```

### On Kubernetes

On EKS, use [IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html).
On other clusters, add the cluster's token issuer to AWS IAM as an OpenID Connect identity provider. In the trust
policy, use the subject `system:serviceaccount:<namespace>:<service-account>` and the audience `sts.amazonaws.com`.

## Grant access to the bucket

**App bucket:** the app deletes old database changes and deleted attachments, so it needs delete rights:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<bucket>"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    }
  ]
}
```

**Backup bucket:** grant only the [backup permissions](../backups/#permissions). No delete.

## Configure the variables

| App bucket | Backup bucket | Value |
| --- | --- | --- |
| `AWS_ROLE_ARN` | `BACKUP_AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/<role>` |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` | Path to the OIDC token. See below for Fly.io and EKS. |
| `AWS_ROLE_SESSION_NAME` | `BACKUP_AWS_ROLE_SESSION_NAME` | Optional. Name shown in CloudTrail. |
| `AWS_REGION` | `BACKUP_AWS_REGION` | Region of the bucket, for example `eu-central-1`. |
| `AWS_ENDPOINT_URL_S3` | `BACKUP_AWS_ENDPOINT_URL_S3` | Unset. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY` | Unset. |

Good to know:

- **Remove the access keys.** If `AWS_ACCESS_KEY_ID` is still set, the app bucket keeps using it and ignores the
  role. For the backup bucket, setting both is an error.
- **Where the token comes from:**
  - *Fly.io:* Fly.io writes the token for the app bucket for you once `AWS_ROLE_ARN` is set. For the backup bucket,
    leave `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` unset; the app asks Fly.io for a token.
  - *EKS:* the token file is set up for you. Point `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` at the same file.
  - *Other clusters:* mount a service account token into the pod (see below).

### Example: Fly.io

Set the roles and regions in `fly.toml`:

```toml title="fly.toml"
[env]
AWS_ROLE_ARN = "arn:aws:iam::<account>:role/vaultwarden"
AWS_REGION = "eu-central-1"
BUCKET_NAME = "<bucket>"
BACKUP_AWS_ROLE_ARN = "arn:aws:iam::<account>:role/vaultwarden-backup"
BACKUP_AWS_REGION = "eu-central-1"
```

Then remove the Tigris secrets that `fly storage create` set. Secrets win over `[env]`, so they would break the
setup:

```sh
fly secrets unset --app <app_name> \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION AWS_ENDPOINT_URL_S3 BUCKET_NAME
```

### Example: Kubernetes

```yaml
spec:
  containers:
    - name: vaultwarden
      env:
        - name: AWS_ROLE_ARN
          value: arn:aws:iam::<account>:role/vaultwarden
        - name: AWS_WEB_IDENTITY_TOKEN_FILE
          value: /var/run/secrets/aws/token
        - name: BACKUP_AWS_ROLE_ARN
          value: arn:aws:iam::<account>:role/vaultwarden-backup
        - name: BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE
          value: /var/run/secrets/aws/token
      volumeMounts:
        - name: aws-token
          mountPath: /var/run/secrets/aws
          readOnly: true
  volumes:
    - name: aws-token
      projected:
        sources:
          - serviceAccountToken:
              audience: sts.amazonaws.com
              expirationSeconds: 3600
              path: token
```

## Recovery

Backup archives store `AWS_ROLE_ARN`, `AWS_ROLE_SESSION_NAME` and `AWS_REGION`. The server you restore to must
provide its own token.
