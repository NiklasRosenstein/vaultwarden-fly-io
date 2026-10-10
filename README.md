<p align="center">
  <img src="./.github/assets/vaultwarden-on-flyio.webp" width="320">
</p>

<h1 align="center">Vaultwarden on Fly.io</h1>

<p align="center">
  Run <a href="https://github.com/dani-garcia/vaultwarden">Vaultwarden</a> on <a href="https://fly.io/">Fly.io</a> for about 2 USD a month.<br>
  SQLite replicated to S3 by <a href="https://litestream.io/">Litestream</a>, attachments and Sends in S3, encrypted recovery backups.
</p>

<p align="center">
  <a href="https://niklasrosenstein.github.io/vaultwarden-fly-io/"><b>Documentation</b></a> ·
  <a href="https://niklasrosenstein.github.io/vaultwarden-fly-io/getting-started/installation/">Installation</a> ·
  <a href="https://niklasrosenstein.github.io/vaultwarden-fly-io/reference/configuration/">Configuration</a>
</p>

## Quick start

```sh
fly app create <app_name>
fly storage create --app <app_name> --name <app_name>
fly secrets set --app <app_name> \
  VAULTWARDEN_RSA_PRIVATE_KEY="$(openssl genrsa 2048)" \
  AGE_SECRET_KEY="$(age-keygen | tail -n1)"
cp fly.example.toml fly.toml   # set app, domain and admin token
fly deploy && fly scale count 1
```

See the [installation guide](https://niklasrosenstein.github.io/vaultwarden-fly-io/getting-started/installation/)
for the full walkthrough.
