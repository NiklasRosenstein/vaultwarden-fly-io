"""Optional, supervised SQLite/file backups; S3 completion records own the schedule."""

import base64
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

LOG = logging.getLogger("backup")
SCHEMA_VERSION = 1
DATA_DIR = Path("/data")
# Only image inputs needed for recovery belong in the encrypted environment export.
RECOVERY_FIELDS = """
AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_REGION AWS_ENDPOINT_URL_S3
BUCKET_NAME AGE_SECRET_KEY GEESEFS_ENABLED GEESEFS_MEMORY_LIMIT LITESTREAM_ENABLED
LITESTREAM_RETENTION LITESTREAM_RETENTION_CHECK_INTERVAL LITESTREAM_VALIDATION_INTERVAL
LITESTREAM_SYNC_INTERVAL ROCKET_PORT
VAULTWARDEN_RSA_PRIVATE_KEY VAULTWARDEN_ADMIN_TOKEN VAULTWARDEN_DOMAIN
VAULTWARDEN_LOG_LEVEL VAULTWARDEN_IP_HEADER VAULTWARDEN_SENDS_ALLOWED VAULTWARDEN_HIBP_API_KEY
VAULTWARDEN_SIGNUPS_ALLOWED VAULTWARDEN_SIGNUPS_VERIFY VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME
VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT VAULTWARDEN_INVITATIONS_ALLOWED
VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED VAULTWARDEN_EMAIL_CHANGE_ALLOWED VAULTWARDEN_PASSWORD_ITERATIONS
VAULTWARDEN_PASSWORD_HINTS_ALLOWED VAULTWARDEN_SHOW_PASSWORD_HINT VAULTWARDEN_INVITATION_ORG_NAME
VAULTWARDEN_DISABLE_2FA_REMEMBER VAULTWARDEN_USE_SENDMAIL VAULTWARDEN_ENABLE_YUBICO
VAULTWARDEN_ENABLE_DUO VAULTWARDEN_ENABLE_SMTP VAULTWARDEN_ENABLE_EMAIL_2FA
VAULTWARDEN_SMTP_HOST VAULTWARDEN_SMTP_FROM VAULTWARDEN_SMTP_USERNAME VAULTWARDEN_SMTP_PASSWORD
VAULTWARDEN_SMTP_SECURITY VAULTWARDEN_SMTP_PORT VAULTWARDEN_SMTP_FROM_NAME
VAULTWARDEN_PUSH_INSTALLATION_ID VAULTWARDEN_PUSH_INSTALLATION_KEY
VAULTWARDEN_YUBICO_CLIENT_ID VAULTWARDEN_YUBICO_SECRET_KEY
""".split()
RECOVERY_EXCLUSIONS = {
    "FLY_APP_NAME": "Recovery records the effective VAULTWARDEN_DOMAIN instead.",
    "ENTRYPOINT_IDLE": "Would prevent the recovered application from starting.",
    "IMPORT_DATABASE": "One-time import must be chosen explicitly during recovery.",
    "BACKUP_ENABLED": "Enable backups explicitly after validating the recovered service.",
    "BACKUP_BUCKET_NAME": "Destination is configured independently during recovery.",
    "BACKUP_PREFIX": "Destination is configured independently during recovery.",
    "BACKUP_AWS_ACCESS_KEY_ID": "Backup-destination credentials are not application recovery data.",
    "BACKUP_AWS_SECRET_ACCESS_KEY": "Backup-destination credentials are not application recovery data.",
    "BACKUP_AWS_SESSION_TOKEN": "Temporary backup credentials are not application recovery data.",
    "BACKUP_AWS_REGION": "Destination is configured independently during recovery.",
    "BACKUP_AWS_ENDPOINT_URL_S3": "Destination is configured independently during recovery.",
    "BACKUP_AGE_RECIPIENT": "Recovery encryption is configured independently of the application.",
    "BACKUP_INTERVAL_SECONDS": "Backup scheduling is configured independently during recovery.",
    "BACKUP_TIMEOUT_SECONDS": "Backup scheduling is configured independently during recovery.",
    "BACKUP_MAX_BYTES": "Backup resource limits are configured for the recovery host.",
    "BACKUP_TMP_DIR": "Temporary storage is configured for the recovery host.",
}


class BackupError(ValueError):
    """A diagnostic written by this module that is safe to include in logs."""


def positive_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        raise BackupError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise BackupError(f"{name} must be positive")
    return value


def required(name):
    value = os.environ.get(name)
    if not value:
        raise BackupError(f"{name} is required")
    return value


def utc(timestamp):
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise BackupError("timestamp needs a timezone")
    return parsed.timestamp()


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


class Store:
    def __init__(self, client, bucket, prefix):
        self.client, self.bucket, self.prefix = client, bucket, prefix.strip("/")

    def key(self, suffix):
        return f"{self.prefix}/{suffix}" if self.prefix else suffix

    def objects(self, suffix):
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.key(suffix)
        ):
            yield from page.get("Contents", [])


def s3_store(backup=False):
    import boto3
    from botocore.config import Config

    # Explicit credentials prevent destination requests from using source AWS credentials.
    prefix = "BACKUP_" if backup else ""
    client = boto3.client(
        "s3",
        aws_access_key_id=required(prefix + "AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=required(prefix + "AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get(prefix + "AWS_SESSION_TOKEN"),
        region_name=required(prefix + "AWS_REGION"),
        endpoint_url=os.environ.get(prefix + "AWS_ENDPOINT_URL_S3"),
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            ignore_configured_endpoint_urls=True,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )
    return Store(
        client,
        required(prefix + "BUCKET_NAME"),
        required("BACKUP_PREFIX") if backup else "data",
    )


def read_completion(store, key, now):
    from botocore.exceptions import ClientError

    response = store.client.get_object(Bucket=store.bucket, Key=key)
    with closing(response["Body"]) as body:
        raw = body.read(65537)
    if len(raw) > 65536:
        raise BackupError("completion manifest too large")
    record = json.loads(raw)
    captured = parse_utc(record["captured_at"])
    if record["schema_version"] != SCHEMA_VERSION or not 0 <= captured <= now:
        raise BackupError("invalid completion manifest")
    if not record["archive_key"].startswith(store.key("archives/")):
        raise BackupError("archive outside backup prefix")
    checksum = record["archive_sha256"]
    if (
        type(record["archive_size"]) is not int
        or record["archive_size"] <= 0
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or any(char not in "0123456789abcdef" for char in checksum)
    ):
        raise BackupError("invalid archive metadata")
    try:
        head = store.client.head_object(Bucket=store.bucket, Key=record["archive_key"])
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("403", "AccessDenied"):
            raise
        # Prefix-scoped ListBucket may leave HEAD unable to distinguish absent from denied.
        # An authorized exact-prefix listing proves absence without granting bucket-wide access.
        listed = store.client.list_objects_v2(
            Bucket=store.bucket, Prefix=record["archive_key"], MaxKeys=1
        )
        if any(
            obj["Key"] == record["archive_key"] for obj in listed.get("Contents", [])
        ):
            raise
        raise BackupError("completed archive is missing") from None
    if (
        head["ContentLength"] != record["archive_size"]
        or head.get("Metadata", {}).get("sha256") != record["archive_sha256"]
    ):
        raise BackupError("completed archive does not match manifest")
    return captured


def latest_capture(store, now):
    from botocore.exceptions import ClientError

    records = sorted(
        (
            obj["Key"]
            for obj in store.objects("completed/")
            if obj["Key"].endswith(".json")
        ),
        reverse=True,
    )
    # IDs start with a UTC capture timestamp. Invalid history must not prevent new captures.
    for key in records:
        try:
            return read_completion(store, key, now)
        except ClientError as error:
            # Access, transport and service failures are not evidence of a missing archive.
            if error.response["Error"]["Code"] not in ("NoSuchKey", "NotFound", "404"):
                raise
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            pass
        LOG.warning("skipping unusable backup completion record")
    return None


def snapshot_database(source, destination):
    # mode=ro refuses to create an empty database if startup/restore has not finished.
    with closing(
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        with closing(sqlite3.connect(destination)) as copy:
            db.backup(copy, pages=256, sleep=0.1)
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("SQLite integrity check failed")
            tables = {
                row[0]
                for row in copy.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"users", "ciphers", "attachments", "sends"} <= tables:
                raise BackupError("not an initialized Vaultwarden database")


def safe_path(root, name):
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in name:
        raise BackupError("unsafe object path")
    return root.joinpath(*parts)


def capture_files(store, destination, remaining_bytes):
    for prefix in ("attachments/", "sends/"):
        for obj in store.objects(prefix):
            key = obj["Key"]
            relative = key.removeprefix(store.key(""))
            if relative == key or not relative.startswith(prefix):
                raise BackupError("object outside source prefix")
            if key.endswith("/") and obj["Size"] == 0:
                continue
            path = safe_path(destination, relative)
            if obj["Size"] > remaining_bytes:
                raise BackupError("backup exceeds BACKUP_MAX_BYTES")
            response = store.client.get_object(
                Bucket=store.bucket, Key=key, IfMatch=obj["ETag"]
            )
            with closing(response["Body"]) as body:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    size = 0
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > obj["Size"]:
                            raise BackupError("object changed during capture")
                        output.write(chunk)
            if size != obj["Size"]:
                raise BackupError("truncated source object")
            remaining_bytes -= size


def validate_files(database, files):
    with closing(sqlite3.connect(database)) as db:
        expected = [
            (f"attachments/{cipher}/{ident}", size)
            for ident, cipher, size in db.execute(
                "SELECT id, cipher_uuid, file_size FROM attachments"
            )
        ]
        for ident, data in db.execute("SELECT uuid, data FROM sends WHERE atype=1"):
            data = {key.lower(): value for key, value in json.loads(data).items()}
            expected.append((f"sends/{ident}/{data['id']}", int(data["size"])))
    missing = []
    for name, size in expected:
        try:
            actual = safe_path(files, name).stat().st_size
        except FileNotFoundError:
            missing.append({"path": name, "expected_size": size})
            continue
        if actual != size:
            raise BackupError("referenced file size mismatch")
    return missing


def capture_recovery(destination):
    config = json.loads((DATA_DIR / "config.json").read_text())
    environment = {
        name: os.environ[name] for name in RECOVERY_FIELDS if name in os.environ
    }
    environment["VAULTWARDEN_DOMAIN"] = config["domain"]
    environment["VAULTWARDEN_RSA_PRIVATE_KEY"] = (DATA_DIR / "rsa_key.pem").read_text()
    write_json(destination / "environment.json", environment)
    write_json(destination / "config.json", config)
    (destination / "rsa_key.pem").write_text(environment["VAULTWARDEN_RSA_PRIVATE_KEY"])


def encrypt_archive(payload, target, recipient):
    with target.open("xb") as output:
        with subprocess.Popen(
            ["age", "-r", recipient],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
        ) as age:
            try:
                with tarfile.open(fileobj=age.stdin, mode="w|gz") as archive:
                    archive.add(payload, arcname="vaultwarden")
            finally:
                age.stdin.close()
            if age.wait() != 0:
                raise BackupError("archive encryption failed")


def publish(store, archive, record):
    checksum = digest(archive)
    archive_key = store.key(f"archives/{record['backup_id']}.tar.age")
    # Single PUT caps the archive at <5 GiB and lets S3 verify the full SHA-256 checksum.
    with archive.open("rb") as body:
        store.client.put_object(
            Bucket=store.bucket,
            Key=archive_key,
            Body=body,
            IfNoneMatch="*",
            ContentLength=archive.stat().st_size,
            ContentType="application/octet-stream",
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=base64.b64encode(checksum.digest()).decode(),
            Metadata={"sha256": checksum.hexdigest()},
        )
    head = store.client.head_object(Bucket=store.bucket, Key=archive_key)
    if (
        head["ContentLength"] != archive.stat().st_size
        or head.get("Metadata", {}).get("sha256") != checksum.hexdigest()
    ):
        raise BackupError("uploaded archive verification failed")
    completion = dict(
        record,
        completed_at=utc(time.time()),
        archive_key=archive_key,
        archive_size=archive.stat().st_size,
        archive_sha256=checksum.hexdigest(),
    )
    manifest = json.dumps(completion).encode()
    store.client.put_object(
        Bucket=store.bucket,
        Key=store.key(f"completed/{record['backup_id']}.json"),
        Body=manifest,
        ContentType="application/json",
        IfNoneMatch="*",
        ChecksumAlgorithm="SHA256",
        ChecksumSHA256=base64.b64encode(hashlib.sha256(manifest).digest()).decode(),
    )


def run_once(work):
    destination = s3_store(backup=True)
    interval = positive_int("BACKUP_INTERVAL_SECONDS", 3600)
    now = time.time()
    previous = latest_capture(destination, now)
    if previous is not None and previous + interval > now:
        return previous + interval
    # Restore and application migrations must finish before opening SQLite.
    with urlopen(
        f"http://127.0.0.1:{os.environ.get('ROCKET_PORT', '8080')}/alive", timeout=5
    ) as response:
        if response.status != 200:
            raise BackupError("Vaultwarden is not ready")
    if os.environ.get("GEESEFS_ENABLED", "true") != "true":
        raise BackupError("scheduled backup requires S3-backed attachments and sends")
    maximum = positive_int("BACKUP_MAX_BYTES", 1024**3)
    if maximum > 4 * 1024**3:
        raise BackupError("BACKUP_MAX_BYTES must be at most 4 GiB")
    recipient = required("BACKUP_AGE_RECIPIENT")
    captured = parse_utc(utc(time.time()))
    backup_id = (
        datetime.fromtimestamp(captured, timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + "-"
        + uuid.uuid4().hex
    )
    payload = work / "payload"
    payload.mkdir(mode=0o700)
    database = payload / "db.sqlite3"
    LOG.info("capturing database")
    source = DATA_DIR / "db.sqlite3"
    if source.stat().st_size > maximum:
        raise BackupError("database exceeds BACKUP_MAX_BYTES")
    snapshot_database(source, database)
    database_finished = time.time()
    if database.stat().st_size > maximum:
        raise BackupError("database exceeds BACKUP_MAX_BYTES")
    LOG.info("capturing files")
    files = payload / "files"
    files.mkdir()
    capture_files(s3_store(), files, maximum - database.stat().st_size)
    missing = validate_files(database, files)
    recovery = payload / "recovery"
    recovery.mkdir()
    capture_recovery(recovery)
    version = (
        subprocess.check_output(["/vaultwarden", "--version"], timeout=10)
        .decode()
        .strip()
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "captured_at": utc(captured),
        "database_capture_finished_at": utc(database_finished),
        "vaultwarden_version": version,
        "status": "degraded" if missing else "complete",
        "missing_file_count": len(missing),
    }
    entries = {
        str(path.relative_to(payload)): {
            "size": path.stat().st_size,
            "sha256": digest(path).hexdigest(),
        }
        for path in payload.rglob("*")
        if path.is_file()
    }
    if sum(entry["size"] for entry in entries.values()) > maximum:
        raise BackupError("backup exceeds BACKUP_MAX_BYTES")
    write_json(
        payload / "manifest.json", dict(record, files=entries, missing_files=missing)
    )
    archive = work / "backup.tar.age"
    LOG.info("encrypting archive")
    encrypt_archive(payload, archive, recipient)
    LOG.info("uploading archive")
    publish(destination, archive, record)
    LOG.log(
        logging.WARNING if missing else logging.INFO,
        "backup published id=%s captured_at=%s status=%s missing_file_count=%s",
        backup_id,
        record["captured_at"],
        record["status"],
        len(missing),
    )
    return captured + interval


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def stop_capture(process):
    # Captures have no persistent local state; terminate age and any other children too.
    try:
        signal_group(process, signal.SIGKILL)
        process.wait(timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        LOG.error(
            "cannot stop backup worker; further captures suspended until it exits"
        )
        return False


def cleanup_staging(staging):
    try:
        staging.cleanup()
        return True
    except OSError:
        LOG.error("cannot remove backup staging; further captures suspended")
        return False


def supervise(command):
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app = subprocess.Popen(command, start_new_session=True)
    capture = None
    staging = None
    due, deadline, failures = 0, 0, 0
    try:
        while not stopping and app.poll() is None:
            now = time.monotonic()
            if capture is not None:
                timed_out = now >= deadline
                if timed_out and not stop_capture(capture):
                    deadline = float("inf")
                if capture.poll() is not None:
                    success = capture.returncode == 0 and not timed_out
                    if success:
                        try:
                            schedule = json.loads(
                                (Path(staging.name) / "schedule.json").read_text()
                            )
                            due = now + max(1, schedule["next_due"] - time.time())
                            failures = 0
                        except (OSError, ValueError, KeyError, TypeError):
                            success = False
                    if not success:
                        failures += 1
                        delay = min(60 * 2 ** min(failures - 1, 4), 900)
                        due = now + delay
                        LOG.error(
                            "backup attempt failed timeout=%s retry_in_seconds=%s",
                            timed_out,
                            delay,
                        )
                    if cleanup_staging(staging):
                        staging = None
                    else:
                        due = float("inf")
                    capture = None
            if capture is None and now >= due:
                try:
                    deadline = now + positive_int("BACKUP_TIMEOUT_SECONDS", 1800)
                    staging = tempfile.TemporaryDirectory(
                        prefix="vaultwarden-backup-",
                        dir=os.environ.get("BACKUP_TMP_DIR"),
                    )
                    capture = subprocess.Popen(
                        [sys.executable, __file__, "once", staging.name],
                        start_new_session=True,
                    )
                except (OSError, ValueError):
                    due = now + 60
                    if staging is not None:
                        if cleanup_staging(staging):
                            staging = None
                        else:
                            due = float("inf")
                    LOG.error("cannot start backup worker")
            time.sleep(0.5)
    finally:
        capture_stopped = capture is None or stop_capture(capture)
        if staging is not None and capture_stopped:
            cleanup_staging(staging)
        if app.poll() is None:
            # Litestream forwards termination to its managed Vaultwarden process.
            app.send_signal(signal.SIGTERM)
            try:
                app.wait(timeout=60)
            except subprocess.TimeoutExpired:
                signal_group(app, signal.SIGKILL)
                app.wait(timeout=10)
        signal_group(app, signal.SIGKILL)
    return app.returncode if app.returncode >= 0 else 128 - app.returncode


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s component=backup %(message)s",
    )
    if sys.argv[1] == "supervise":
        return supervise(sys.argv[2:])
    if sys.argv[1] == "once":
        os.umask(0o077)
        try:
            work = Path(sys.argv[2])
            write_json(work / "schedule.json", {"next_due": run_once(work)})
            return 0
        except Exception as error:
            # SDK/SQLite exceptions can contain credentials, URLs or account data.
            if isinstance(error, BackupError):
                LOG.error("backup failed: %s", error)
            else:
                LOG.error("backup failed error_type=%s", type(error).__name__)
            return 1
    raise BackupError("expected supervise or once")


if __name__ == "__main__":
    sys.exit(main())
