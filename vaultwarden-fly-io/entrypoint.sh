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

S3_MONITOR_FAILED_MARKER=/tmp/s3-monitor-failed

check_s3() {
  # Returns 0 if the GeeseFS mount at /mnt/s3 looks healthy, 1 if it is degraded (possibly transient) and 2 if it is
  # definitely broken (GeeseFS process gone or the mount disappeared).
  if ! pgrep -x geesefs >/dev/null; then
    error "s3-monitor: geesefs process is not running"
    return 2
  fi
  if ! grep -q " /mnt/s3 fuse" /proc/mounts; then
    error "s3-monitor: /mnt/s3 is not mounted"
    return 2
  fi
  # Listing the directory goes to S3 once GeeseFS' stat cache expires, so this also catches a broken connection. A dead
  # FUSE mount can hang indefinitely, hence the timeout.
  if ! timeout -s KILL "$GEESEFS_MONITOR_TIMEOUT" ls /mnt/s3 >/dev/null 2>&1; then
    warn "s3-monitor: listing /mnt/s3 failed or did not complete within ${GEESEFS_MONITOR_TIMEOUT}s"
    return 1
  fi
  return 0
}

monitor_s3() {
  # Periodically check that /mnt/s3 still works. If it looks unrecoverable, terminate the main process (given as $1) so
  # that the container exits with a non-zero status and Fly.io restarts the machine.
  trap - EXIT
  main_pid="$1"
  failures=0
  info "s3-monitor: started (interval ${GEESEFS_MONITOR_INTERVAL}s, timeout ${GEESEFS_MONITOR_TIMEOUT}s," \
    "failure threshold ${GEESEFS_MONITOR_FAILURE_THRESHOLD})"
  while kill -0 "$main_pid" 2>/dev/null; do
    sleep "$GEESEFS_MONITOR_INTERVAL"
    status=0
    check_s3 || status=$?
    if [ "$status" -eq 0 ]; then
      if [ "$failures" -gt 0 ]; then
        info "s3-monitor: /mnt/s3 recovered after $failures failed check(s)"
      fi
      failures=0
      continue
    fi
    failures=$((failures + 1))
    if [ "$status" -eq 1 ] && [ "$failures" -lt "$GEESEFS_MONITOR_FAILURE_THRESHOLD" ]; then
      warn "s3-monitor: check failed ($failures/$GEESEFS_MONITOR_FAILURE_THRESHOLD)"
      continue
    fi
    error "s3-monitor: /mnt/s3 looks unrecoverable, terminating to force a restart of the machine"
    touch "$S3_MONITOR_FAILED_MARKER"
    kill -TERM "$main_pid" 2>/dev/null || true
    # Give Litestream a chance to shut down gracefully and push its last frames before we force it.
    sleep 30
    kill -KILL "$main_pid" 2>/dev/null || true
    return
  done
}

run_main() {
  # Run the given command in the background, forwarding termination signals to it, and supervise /mnt/s3 next to it.
  # We can't just exec the command, as we need to exit with a non-zero status when the S3 monitor kicks in for Fly.io
  # to restart the machine.
  rm -f "$S3_MONITOR_FAILED_MARKER"
  info "$@"
  "$@" &
  main_pid=$!
  trap 'kill -TERM $main_pid 2>/dev/null' INT TERM

  monitor_pid=
  if [ "${GEESEFS_ENABLED:-true}" = "true" ] && [ "${GEESEFS_MONITOR_ENABLED:-true}" = "true" ]; then
    GEESEFS_MONITOR_INTERVAL=${GEESEFS_MONITOR_INTERVAL:-30}
    GEESEFS_MONITOR_TIMEOUT=${GEESEFS_MONITOR_TIMEOUT:-20}
    GEESEFS_MONITOR_FAILURE_THRESHOLD=${GEESEFS_MONITOR_FAILURE_THRESHOLD:-3}
    monitor_s3 "$main_pid" &
    monitor_pid=$!
  fi

  # `wait` returns early when a trapped signal arrives, so keep waiting until the process has actually exited.
  status=0
  wait "$main_pid" || status=$?
  while kill -0 "$main_pid" 2>/dev/null; do
    status=0
    wait "$main_pid" || status=$?
  done
  trap - INT TERM

  if [ -n "$monitor_pid" ]; then
    kill "$monitor_pid" 2>/dev/null || true
  fi
  if [ -f "$S3_MONITOR_FAILED_MARKER" ]; then
    error "exiting because the S3 monitor detected a broken /mnt/s3 mount"
    trap - EXIT
    exit 1
  fi
  trap - EXIT
  exit "$status"
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
  "ip_header": "${VAULTWARDEN_IP_HEADER:-X-Real-IP}",
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
  if [ "${BACKUP_ENABLED:-false}" = "true" ]; then
    run_main python3 /backup.py supervise /litestream-entrypoint.sh /vaultwarden
  fi
  run_main /litestream-entrypoint.sh "/vaultwarden"
}

main "$@"
