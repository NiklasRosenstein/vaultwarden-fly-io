# Vaultwarden on Fly.io

Run [Vaultwarden] on [Fly.io] on ephemeral storage with reliable [Litestream] SQlite replication and attachments
stored in S3.

  [Vaultwarden]: https://github.com/dani-garcia/vaultwarden
  [Fly.io]: https://fly.io/
  [Litestream]: https://litestream.io/


## Prerequisites

* An account on [Fly.io]
* The [fly](https://github.com/superfly/flyctl) CLI
* The [age](https://github.com/FiloSottile/age) CLI

## Advanced topics

### Vaultwarden on ephemeral disk

The [Backing up your Vault](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault) documentation for
Vaultwarden explains the purpose of each the files and directories in the `/data` directory. Since we're running
on an ephemeral disk, we need to have an alternative story around the persistence of these files.

  [GeeseFS]: https://github.com/yandex-cloud/geesefs/

| Path                          | Persistence implementation                                                   |
|-------------------------------|------------------------------------------------------------------------------|
| `/data/attachments`           | Mounted to the S3 bucket via [GeeseFS].                                      |
| `/data/config.json`           | Auto-generated on startup from environment variables.                        |
| `/data/db.sqlite{,-shm,-wal}` | Replicated to S3 via [Litestream].                                           |
| `/data/icon_cache`            | Mounted to the S3 bucket via [GeeseFS].                                      |
| `/data/rsa_key.pem`           | Written to disk from the `VAULTWARDEN_RSA_PRIVATE_KEY` environment variable. |
| `/data/rsa_key.pub.pem`       | Auto-generated from the `VAULTWARDEN_RSA_PRIVATE_KEY` environment variable.  |
| `/data/sends`                 | Mounted to the S3 bucket via [GeeseFS].                                      |
