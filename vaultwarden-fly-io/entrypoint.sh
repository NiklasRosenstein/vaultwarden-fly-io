#!/bin/sh

set -eu

#
# Utility functions
#

info() {
  >&2 echo "[$0 |  INFO]:" "$@"
}

warn() {
  >&2 echo "[$0 |  WARN]:" "$@"
}

error() {
  >&2 echo "[$0 | ERROR]:" "$@"
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

#
# Business logic
#

VAULTWARDEN_CONFIG_PATH=/data/config.json

mount_s3() {
  # Mount data directories that should be stored in S3. Note that we do not need to use SSE-C because Vaultwarden
  # already encrypts these data files (except for the icon cache, but who cares).
  if [ "${GEESEFS_ENABLED:-true}" = "true" ]; then
    # NOTE: We configure Vaultwarden from the default data directory paths (e.g. /data/attachments, /data/icon_cache)
    #       to directories inside /data/files instead, for two reasons:
    #       (1) Vaultwarden's startup procedure uses std::fs::create_dir_all() which seems to error if the directory
    #           already exists; I can't explain how this does NOT error when Vaultwarden is run with a persistent
    #           disk that does not use mounts for these directories, but something behaves differently if the actual
    #           target directory is a mount.
    #       (2) This allows GeeseFS to share the same memory limit across all data files served and we only need to
    #           spawn a single GeeseFS process.
    #       Also, using --uid 100 causes that even root gets permission errors when accessing the directory, so instead
    #       we do not use this option, keep the file owner as root and run Vaultwarden as root (:sadface:).
    info "setting up S3 mountpoints"
    mkdir -p /mnt/s3
    GEESEFS_MEMORY_LIMIT=${GEESEFS_MEMORY_LIMIT:-64}
    info_run sudo -E geesefs --memory-limit "$GEESEFS_MEMORY_LIMIT" --endpoint "$AWS_ENDPOINT_URL_S3" "$BUCKET_NAME:data/" /mnt/s3
  else
    warn "GeeseFS is disabled, certain data directories are not persisted."
  fi
}

write_rsa_key() {
  # Write the RSA key that is used to sign authentication tokens.
  info "writing /data/rsa_key.pem and /data/rsa_key.pub.pem"
  assert_is_set VAULTWARDEN_RSA_PRIVATE_KEY
  echo "$VAULTWARDEN_RSA_PRIVATE_KEY" >/data/rsa_key.pem
  openssl rsa -in /data/rsa_key.pem -pubout >/data/rsa_key.pub.pem
}

write_config() {
  # Generate admin configuration from environment variables.
  VAULTWARDEN_DOMAIN="${VAULTWARDEN_DOMAIN:-https://${FLY_APP_NAME}.fly.dev}"
  assert_is_set VAULTWARDEN_ADMIN_TOKEN

  cat <<EOF >$VAULTWARDEN_CONFIG_PATH
{
  "log_level": "${VAULTWARDEN_LOG_LEVEL:-info}",
  "log_timestamp_format": "%Y-%m-%d %H:%M:%S.%3f",
  "enable_db_wal": true,
  "attachments_folder": "/mnt/s3/attachments",
  "icon_cache_folder": "/mnt/s3/icon_cache",
  "sends_folder": "/mnt/s3/sends",
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
  "smtp_port": ${VAULTWARDEN_SMTP_PORT:-465},
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

  if [ -n "${VAULTWARDEN_PUSH_INSTALLATION_ID:-}" ]; then
    assert_is_set VAULTWARDEN_PUSH_INSTALLATION_KEY
    cat <<EOF >>$VAULTWARDEN_CONFIG_PATH
  "push_installation_id": "${VAULTWARDEN_PUSH_INSTALLATION_ID}",
  "push_installation_key": "${VAULTWARDEN_PUSH_INSTALLATION_KEY}",
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

  # Prevent writing to the config.json, the admin panel should only serve as point to view settings.
  chmod -w $VAULTWARDEN_CONFIG_PATH
}

validate_config() {
  # Validate the JSON file syntax. This is a sanity check that should prevent successful startup if we made a mistake
  # in the JSON snytax, as Vaultwarden will not complain and simply not load the file.
  info "validating $VAULTWARDEN_CONFIG_PATH syntax"
  if ! jq < $VAULTWARDEN_CONFIG_PATH >/dev/null; then
    error "we made a mistake in $VAULTWARDEN_CONFIG_PATH, please file a bug report"
    exit 1
  fi
}

main() {
  mount_s3
  write_rsa_key
  write_config
  validate_config
  maybe_idle
  export I_REALLY_WANT_VOLATILE_STORAGE=true
  export BUCKET_PATH="vaultwarden.db"
  export LITESTREAM_DATABASE_PATH=/data/db.sqlite3
  info_run exec /litestream-entrypoint.sh "/vaultwarden"
}

main "$@"
