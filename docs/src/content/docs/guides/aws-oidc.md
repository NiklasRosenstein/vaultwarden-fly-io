---
title: AWS S3 without access keys
description: Store the vault in an AWS S3 bucket with temporary credentials from OIDC instead of long-lived access keys.
---

By default the app's bucket is reached with static access keys, for example the ones `fly storage create` sets. If
the bucket is in AWS S3, the app can use an IAM role instead. The machine or pod presents an OIDC token, and AWS STS
exchanges it for temporary credentials through
[`AssumeRoleWithWebIdentity`](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html):

```
OIDC token file → AWS STS → temporary AWS credentials → application bucket
```

GeeseFS, Litestream and the entrypoint all use the AWS SDK's standard credential chain. They pick the role up from
`AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE`, and refresh the credentials before they expire. Nothing in the
image needs to be switched on.

:::note
This only works with AWS S3. Other providers, including Tigris from `fly storage create`, don't accept AWS STS
credentials.
:::

## Permissions

Create the bucket in AWS S3, and a role whose permissions policy covers the whole bucket. Litestream and GeeseFS
delete objects (expired WAL segments, deleted attachments), so the role needs `DeleteObject` too.

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

The role's trust policy depends on where the app runs, see below.

## Configure the app

Remove the static keys. If `AWS_ACCESS_KEY_ID` is set, the AWS SDKs use it and ignore the role.

| Variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | `arn:aws:iam::<account>:role/<role>` |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | Path to the OIDC token. Set automatically on Fly.io and on EKS. |
| `AWS_REGION` | Region of the bucket, for example `eu-central-1`. |
| `AWS_ENDPOINT_URL_S3` | Unset. The image uses the AWS S3 endpoint of `AWS_REGION`. |
| `BUCKET_NAME` | Name of the bucket. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Unset. |

### On Fly.io

Fly.io issues OIDC tokens to every machine (see [OpenID Connect](https://fly.io/docs/security/openid-connect/)). When
`AWS_ROLE_ARN` is set, Fly.io writes a fresh token to `/.fly/oidc_token` every few minutes and sets
`AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_ROLE_SESSION_NAME` for you. The entrypoint waits up to 30 seconds for the
token file to appear after the machine starts.

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

3. Set the role and region in `fly.toml`, and remove the secrets of the Tigris bucket:

   ```toml title="fly.toml"
   [env]
   AWS_ROLE_ARN = "arn:aws:iam::<account>:role/<role>"
   AWS_REGION = "eu-central-1"
   BUCKET_NAME = "<bucket>"
   ```

   ```sh
   fly secrets unset --app <app_name> AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ENDPOINT_URL_S3 BUCKET_NAME
   ```

### On Kubernetes

On EKS, [IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html)
sets `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` in the pod once the service account is annotated with the role.

On other clusters, add the cluster's service account issuer to AWS IAM as an OpenID Connect identity provider, project
a service account token with the audience `sts.amazonaws.com` into the pod, and set both variables yourself:

```yaml
spec:
  containers:
    - name: vaultwarden
      env:
        - name: AWS_ROLE_ARN
          value: arn:aws:iam::<account>:role/<role>
        - name: AWS_WEB_IDENTITY_TOKEN_FILE
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

The trust policy's subject is `system:serviceaccount:<namespace>:<service-account>`.

## Backups and recovery

The [backup worker](../backups/) reads attachments and Sends with the same role, which the permissions above
already cover. Its destination is configured separately, and can use OIDC too (see
[Authenticate with OIDC](../backups/#authenticate-with-oidc)). Use a different role for it, so that the application
can't delete its own backups.

Recovery archives record `AWS_ROLE_ARN`, `AWS_ROLE_SESSION_NAME` and `AWS_REGION`, but not the token file, which the
recovery host provides.
