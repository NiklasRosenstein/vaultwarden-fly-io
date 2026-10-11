---
title: AWS S3 without access keys
description: Reach your buckets with short-lived keys from an IAM role instead of stored access keys.
---

Fly.io and Kubernetes give each machine or pod a signed identity token. AWS can trade that token for short-lived keys
through an IAM role, so you don't need to store access keys. This works for the app bucket, the
[backup](../../keep-your-data-safe/turn-on-backups/) bucket, or both.

:::note
This only works with AWS S3, not with Tigris or other providers.
:::

## 1. Create the roles

Create one IAM role per bucket, so the app can't delete its own backups.

**On Fly.io**, add an OpenID Connect identity provider in AWS IAM with the URL `https://oidc.fly.io/<org-slug>` and the
audience `sts.amazonaws.com`. Then give each role this trust policy:

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

**On Kubernetes**, use [IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html)
on EKS. On other clusters, add the cluster's token issuer as an identity provider, with the subject
`system:serviceaccount:<namespace>:<service-account>`.

## 2. Grant access

The **app role** needs list on the bucket, and read, write and delete on its objects:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>" },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    }
  ]
}
```

The **backup role** needs only the [backup permissions](../../keep-your-data-safe/turn-on-backups/#permissions).

## 3. Set the variables

| App bucket | Backup bucket | Value |
| --- | --- | --- |
| `AWS_ROLE_ARN` | `BACKUP_AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/<role>` |
| `AWS_REGION` | `BACKUP_AWS_REGION` | Region of the bucket, for example `eu-central-1`. |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | `BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE` | Leave unset on Fly.io. Elsewhere, the path to the token. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3` | `BACKUP_AWS_ACCESS_KEY_ID`, `BACKUP_AWS_SECRET_ACCESS_KEY`, `BACKUP_AWS_ENDPOINT_URL_S3` | Unset. |

On Fly.io you don't need token files. For the app bucket, Fly.io writes the token to a file and sets
`AWS_WEB_IDENTITY_TOKEN_FILE` for you. For the backup bucket, the app asks the machine's local Fly.io API for a fresh
token each time it needs one.

On Fly.io, set the roles, regions and `BUCKET_NAME` in `fly.toml`, then remove the secrets that `fly storage create` added. Secrets win over
`[env]`, so they would break the setup:

```sh
fly secrets unset --app <app_name> \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION AWS_ENDPOINT_URL_S3 BUCKET_NAME
```

On Kubernetes outside EKS, mount a service account token with the audience `sts.amazonaws.com` into the pod. Point
both token file variables at it.
