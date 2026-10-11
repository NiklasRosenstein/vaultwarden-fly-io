---
title: AWS S3 without access keys
description: Reach the application and backup buckets with temporary credentials from OIDC instead of long-lived access keys.
---

By default the buckets are reached with static access keys, for example the ones `fly storage create` sets. If a
bucket is in AWS S3, an IAM role can be used instead. The machine or pod presents an OIDC token, and AWS STS
exchanges it for temporary credentials through
[`AssumeRoleWithWebIdentity`](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html):

```
OIDC token (Fly.io machine or Kubernetes service account) → AWS STS → temporary AWS credentials → bucket
```

This works for the application bucket (`AWS_*` variables) and the [backup](../backups/) destination (the same
variables with a `BACKUP_` prefix), independently of each other. Credentials are refreshed before they expire.

:::note
This only works with AWS S3. Other providers, including Tigris from `fly storage create`, don't accept AWS STS
credentials.
:::

## Trust the token issuer

Create an IAM role for each bucket, and let it be assumed with your platform's OIDC tokens. Use separate roles, so
that the application can't delete its own backups.

### On Fly.io

Fly.io issues OIDC tokens to every machine (see [OpenID Connect](https://fly.io/docs/security/openid-connect/)).

1. In AWS IAM, add an OpenID Connect identity provider with the URL `https://oidc.fly.io/<org-slug>` and the audience
   `sts.amazonaws.com`.
2. Give the role a trust policy for that provider. The token's subject is `<org-slug>:<app-name>:<machine-name>`, so
   restrict it to your app:

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
On other clusters, add the cluster's service account issuer to AWS IAM as an OpenID Connect identity provider. The
trust policy's subject is `system:serviceaccount:<namespace>:<service-account>`, and the audience is
`sts.amazonaws.com`.

## Grant access to the bucket

**Application bucket:** Litestream and GeeseFS delete objects (expired WAL segments, deleted attachments), so the
role needs `DeleteObject` on the whole bucket:

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

**Backup bucket:** grant the [destination permissions](../backups/#permissions) only. The worker never deletes.

## Configure the variables

| Application bucket | Backup bucket | Value |
| --- | --- | --- |
| `AWS_ROLE_ARN` | `BACKUP_AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/<role>` |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` | Path to the OIDC token. See below for Fly.io and EKS. |
| `AWS_ROLE_SESSION_NAME` | `BACKUP_AWS_ROLE_SESSION_NAME` | Optional name shown in CloudTrail. |
| `AWS_REGION` | `BACKUP_AWS_REGION` | Region of the bucket, for example `eu-central-1`. |
| `AWS_ENDPOINT_URL_S3` | `BACKUP_AWS_ENDPOINT_URL_S3` | Unset, so that the AWS S3 endpoint of the region is used. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY` | Unset. |

The two sets are separate: the backup worker never uses the application's `AWS_*` variables for its destination.
They differ in a few details:

- **Access keys:** the AWS SDKs silently prefer `AWS_ACCESS_KEY_ID` over a role, so the application bucket keeps
  using the keys until you remove them. For the backup bucket, setting both is an error.
- **Token file:** read again on every refresh, so tokens rotated in place keep working.
  - *Fly.io, application bucket:* setting `AWS_ROLE_ARN` makes Fly.io write a fresh token to `/.fly/oidc_token`
    every few minutes and set `AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ROLE_SESSION_NAME`. The entrypoint waits up to
    30 seconds for the file to appear after the machine starts.
  - *Fly.io, backup bucket:* leave `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` unset. The worker requests a token from the
    machine API for each refresh.
  - *EKS:* annotating the service account sets `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` in the pod. For the
    backup bucket, point `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` at the same file.
  - *Other clusters:* project a service account token into the pod (see below).

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

Then remove the secrets that `fly storage create` set for the Tigris bucket. Secrets take precedence over `[env]`,
so a leftover `AWS_REGION=auto` would break the AWS endpoint:

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

Recovery archives record `AWS_ROLE_ARN`, `AWS_ROLE_SESSION_NAME` and `AWS_REGION`, but not the token file, which the
recovery host provides. The `BACKUP_*` settings aren't recorded.
