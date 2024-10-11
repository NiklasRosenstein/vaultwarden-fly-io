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

if [ "${ENTRYPOINT_DEBUG:-}" = "true" ]; then
    debug "ENTRYPOINT_DEBUG is set: set -x"
    set -x
fi

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
