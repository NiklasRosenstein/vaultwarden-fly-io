#!/bin/sh

set -eu

debug() {
    >&2 echo "[ entrypoint - DEBUG ]" "$@"
}

info() {
    >&2 echo "[ entrypoint - INFO ]" "$@"
}

error() {
    >&2 echo "[ entrypoint - ERROR ]" "$@"
}

info_run() {
    info "$@"
    "$@"
}

assert_is_set() {
    eval "val=\${$1+x}"
    if [ -z "$val" ]; then
        error "missing expected environment variable \"$1\""
        exit 1
    fi
}

assert_file_exists() {
    if [ ! -f "$1" ]; then
        error "missing expected file \"$1\""
        exit 1
    fi
}

maybe_idle() {
    if [ "${ENTRYPOINT_IDLE:-false}" = "true" ]; then
        info "ENTRYPOINT_IDLE=true, entering idle state"
        sleep infinity
    fi
}

on_error() {
    [ $? -eq 0 ] && exit
    error "an unexpected error occurred."
    maybe_idle
}

trap 'on_error' EXIT

VAULTWARDEN_DB_PATH=/data/db.sqlite3

# These should be available automatically simply by enabling the Fly.io Tigris object storage extension.
assert_is_set AWS_ACCESS_KEY_ID
assert_is_set AWS_SECRET_ACCESS_KEY
assert_is_set AWS_REGION
assert_is_set AWS_ENDPOINT_URL_S3
assert_is_set BUCKET_NAME
assert_is_set AGE_SECRET_KEY

export LITESTREAM_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID"
export LITESTREAM_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"
AGE_PUBLIC_KEY="$(echo "$AGE_SECRET_KEY" | age-keygen -y)"

info "generating /etc/litestream.yml"
cat <<EOF >/etc/litestream.yml
dbs:
- path: "${VAULTWARDEN_DB_PATH}"
  replicas:
  # See https://litestream.io/reference/config/#s3-replica
  - type: s3
    bucket: $BUCKET_NAME
    path: vaultwarden.db
    region: $AWS_REGION
    endpoint: $AWS_ENDPOINT_URL_S3
    # See https://litestream.io/reference/config/#encryption
    age:
      identities:
      - "$AGE_SECRET_KEY"
      recipients:
      - "$AGE_PUBLIC_KEY"
    # See https://litestream.io/reference/config/#retention-period
    retention: "${LITESTREAM_RETENTION:-24h}"
    retention-check-interval: "${LITESTREAM_RETENTION_CHECK_INTERVAL:-1h}"
    # https://litestream.io/reference/config/#validation-interval
    validation-interval: "${LITESTREAM_VALIDATION_INTERVAL:-12h}"
EOF

info "configuring mc"
mc alias set s3 "$AWS_ENDPOINT_URL_S3" "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY"

# Mount data directories that should be stored in S3. Note that we do not need to use SSE-C because Vaultwarden
# already encrypts these data files (except for the icon cache, but who cares).
info "setting up S3 mountpoints"
mkdir -p /data/attachments /data/icon_cache /data/sends
GEESEFS_MEMORY_LIMIT=${GEESEFS_MEMORY_LIMIT:-32}
info_run sudo -E geesefs --memory-limit "$GEESEFS_MEMORY_LIMIT" --endpoint "$AWS_ENDPOINT_URL_S3" "$BUCKET_NAME:data/attachments" /data/attachments
info_run sudo -E geesefs --memory-limit "$GEESEFS_MEMORY_LIMIT" --endpoint "$AWS_ENDPOINT_URL_S3" "$BUCKET_NAME:data/icon_cache" /data/icon_cache
info_run sudo -E geesefs --memory-limit "$GEESEFS_MEMORY_LIMIT" --endpoint "$AWS_ENDPOINT_URL_S3" "$BUCKET_NAME:data/sends" /data/sends

# Write the RSA key that is used to sign authentication tokens.
info "writing /data/rsa_key.pem and /data/rsa_key.pub.pem"
assert_is_set VAULTWARDEN_RSA_PRIVATE_KEY
echo "$VAULTWARDEN_RSA_PRIVATE_KEY" >/data/rsa_key.pem
openssl rsa -in /data/rsa_key.pem -pubout >/data/rsa_key.pub.pem

# Generate admin configuration from environment variables.
VAULTWARDEN_CONFIG_PATH=/data/config.json
VAULTWARDEN_DOMAIN="${VAULTWARDEN_DOMAIN:-https://${FLY_APP_NAME}.fly.dev}"
assert_is_set VAULTWARDEN_ADMIN_TOKEN
cat <<EOF >$VAULTWARDEN_CONFIG_PATH
{
  "domain": "${VAULTWARDEN_DOMAIN}",
  "sends_allowed": ${VAULTWARDEN_SENDS_ALLOWED:-true},
  "hibp_api_key": "${VAULTWARDEN_HIBP_API_KEY:-}",
  "incomplete_2fa_time_limit": 3,
  "disable_icon_download": false,
  "signups_allowed": ${VAULTWARDEN_SIGNUPS_ALLOWED:-true},
  "signups_verify": ${VAULTWARDEN_SIGNUPS_VERIFY:-false},
  "signups_verify_resend_time": ${VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME:-3600},
  "signups_verify_resend_limit": ${VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT:-6},
  "invitations_allowed": ${VAULTWARDEN_INVITATIONS_ALLOWED:-true},
  "emergency_access_allowed": ${VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED:-true},
  "email_change_allowed": ${VAULTWARDEN_EMAIL_CHANGE_ALLOWED:-true},
  "password_iterations": ${VAULTWARDEN_PASSWORD_ITERATIONS:-600000},
  "password_hints_allowed": ${VAULTWARDEN_PASSWORD_HINTS_ALLOWED:-true},
  "show_password_hint": ${VAULTWARDEN_SHOW_PASSWORD_HINT:-false},
  "admin_token": "${VAULTWARDEN_ADMIN_TOKEN}",
  "invitation_org_name": "${VAULTWARDEN_INVITATION_ORG_NAME:-Vaultwarden}",
  "ip_header": "X-Real-IP",
  "icon_redirect_code": 302,
  "icon_cache_ttl": 2592000,
  "icon_cache_negttl": 259200,
  "icon_download_timeout": 10,
  "icon_blacklist_non_global_ips": true,
  "disable_2fa_remember": ${VAULTWARDEN_DISABLE_2FA_REMEMBER:-false},
  "authenticator_disable_time_drift": false,
  "require_device_email": false,
  "reload_templates": false,
  "log_timestamp_format": "%Y-%m-%d %H:%M:%S.%3f",
  "use_sendmail": ${VAULTWARDEN_USE_SENDMAIL:-false},
  "_enable_yubico": ${VAULTWARDEN_ENABLE_YUBICO:-false},
  "_enable_duo": ${VAULTWARDEN_ENABLE_DUO:-false},
  "_enable_smtp": ${VAULTWARDEN_ENABLE_SMTP:-false},
  "_enable_email_2fa": ${VAULTWARDEN_ENABLE_EMAIL_2FA:-${VAULTWARDEN_ENABLE_SMTP:-false}},
EOF

if [ "${VAULTWARDEN_ENABLE_SMTP:-false}" = "true" ]; then
    assert_is_set VAULTWARDEN_SMTP_HOST
    assert_is_set VAULTWARDEN_SMTP_FROM
    assert_is_set VAULTWARDEN_SMTP_USERNAME
    assert_is_set VAULTWARDEN_SMTP_PASSWORD
    cat <<EOF >>$VAULTWARDEN_CONFIG_PATH
  "smtp_host": "${VAULTWARDEN_SMTP_HOST}",
  "smtp_security": "${VAULTWARDEN_SMTP_SECURITY:-force_tls}",
  "smtp_port": "${VAULTWARDEN_SMTP_PORT:-465},
  "smtp_from": "${VAULTWARDEN_SMTP_FROM}",
  "smtp_from_name": "${VAULTWARDEN_SMTP_FROM_NAME:-Vaultwarden}",
  "smtp_username": "${VAULTWARDEN_SMTP_USERNAME}",
  "smtp_password": "${VAULTWARDEN_SMTP_PASSWORD}",
  "smtp_timeout": 15,
  "smtp_embed_images": true,
  "smtp_accept_invalid_certs": false,
  "smtp_accept_invalid_hostnames": false,
  "email_token_size": 6,
  "email_expiration_time": 600,
  "email_attempts_limit": 3,
EOF
fi

if [ "${VAULTWARDEN_ENABLE_YUBICO:-false}" = "true" ]; then
    assert_is_set VAULTWARDEN_YUBICO_CLIENT_ID
    assert_is_set VAULTWARDEN_YUBICO_SECRET_KEY
    cat <<EOF >>$VAULTWARDEN_CONFIG_PATH
  "yubico_client_id": "${VAULTWARDEN_YUBICO_CLIENT_ID}",
  "yubico_secret_key": "${VAULTWARDEN_YUBICO_SECRET_KEY}",
EOF
fi

cat <<EOF >>$VAULTWARDEN_CONFIG_PATH
  "admin_session_lifetime": 20
}
EOF

# Check if there is an existing database to import from S3.
if [ "${IMPORT_DATABASE:-}" = "true" ] && mc find "s3/$BUCKET_NAME/import-db.sqlite" 2> /dev/null > /dev/null; then
    info "found \"import-db.sqlite\" in bucket, importing that database instead of restoring with litestream"
    mc cp "s3/$BUCKET_NAME/import-db.sqlite" "$VAULTWARDEN_DB_PATH"
elif [ "${LITESTREAM_ENABLED:-true}" = "true" ]; then
    info_run litestream restore -if-db-not-exists -if-replica-exists -replica s3 "$VAULTWARDEN_DB_PATH"
fi

maybe_idle

# Run Vaultwarden.
export I_REALLY_WANT_VOLATILE_STORAGE=true
if [ "${LITESTREAM_ENABLED:-true}" = "true" ]; then
    info_run exec litestream replicate -exec "vaultwarden"
else
    info_run exec vaultwarden
fi
