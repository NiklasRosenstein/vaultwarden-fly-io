"""Container entrypoint: mount S3, write configuration, restore and run Vaultwarden.

GeeseFS, Litestream and boto3 resolve S3 credentials from the standard AWS_* variables:
static keys, or AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE for web identity.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from enum import IntEnum
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

LOG = logging.getLogger("entrypoint")
DATA_DIR = Path("/data")
S3_MOUNT = Path("/mnt/s3")
LITESTREAM_CONFIG = Path("/etc/litestream.yml")
VAULTWARDEN = "/vaultwarden"
BACKUP = Path(__file__).with_name("backup.py")
# Key prefix of the Litestream replica in the bucket.
REPLICA_PATH = "vaultwarden.db"
# Time Litestream gets to push its last frames when the S3 monitor restarts the machine.
# This matches the time the backup supervisor (backup.py) gives Litestream when stopped.
RESTART_GRACE_SECONDS = 60


class StartupError(Exception):
    """A configuration or startup problem with a message that is safe to log."""


def env(name: str, default: str = "") -> str:
    # Like the shell's ${NAME:-default}: an empty value counts as unset.
    return os.environ.get(name) or default


def required(name: str) -> str:
    value = env(name)
    if not value:
        raise StartupError(f'missing expected environment variable "{name}"')
    return value


def flag(name: str, default: bool) -> bool:
    value = env(name, "true" if default else "false")
    if value not in ("true", "false"):
        raise StartupError(f'{name} must be "true" or "false", got "{value}"')
    return value == "true"


def integer(name: str, default: int, *, minimum: int = 0) -> int:
    value = env(name, str(default))
    if not value.isdigit() or int(value) < minimum:
        raise StartupError(
            f'{name} must be an integer of at least {minimum}, got "{value}"'
        )
    return int(value)


def positive_int(name: str, default: int) -> int:
    return integer(name, default, minimum=1)


def run(command: Sequence[str]) -> None:
    LOG.info("$ %s", " ".join(command))
    subprocess.run(command, check=True)


#
# Startup
#


def mount_s3() -> None:
    """Mount the bucket's data/ prefix, which holds attachments, Sends and icons."""
    # Vaultwarden uses directories inside /mnt/s3 instead of mounts at its default data
    # directories: its startup fails in create_dir_all() if they are mount points, and a
    # single GeeseFS process shares one memory limit across all of them. Vaultwarden runs
    # as root because GeeseFS' --uid option breaks permissions for the mount entirely.
    if not flag("GEESEFS_ENABLED", True):
        LOG.warning("GeeseFS is disabled, certain data directories are not persisted.")
        return
    command = [
        "geesefs",
        "--memory-limit",
        str(positive_int("GEESEFS_MEMORY_LIMIT", 64)),
    ]
    endpoint = env("AWS_ENDPOINT_URL_S3")
    if not endpoint:
        # GeeseFS defaults to Yandex Object Storage, so AWS S3 needs an explicit endpoint.
        region = required("AWS_REGION")
        endpoint = f"https://s3.{region}.amazonaws.com"
        command += ["--region", region]
    S3_MOUNT.mkdir(parents=True, exist_ok=True)
    run(
        [
            *command,
            "--endpoint",
            endpoint,
            f"{required('BUCKET_NAME')}:data/",
            str(S3_MOUNT),
        ]
    )


def write_rsa_key() -> None:
    """Write the RSA key that is used to sign authentication tokens."""
    LOG.info("writing %s/rsa_key.pem and %s/rsa_key.pub.pem", DATA_DIR, DATA_DIR)
    private = DATA_DIR / "rsa_key.pem"
    private.write_text(required("VAULTWARDEN_RSA_PRIVATE_KEY") + "\n")
    run(
        [
            "openssl",
            "rsa",
            "-in",
            str(private),
            "-pubout",
            "-out",
            str(DATA_DIR / "rsa_key.pub.pem"),
        ]
    )


def vaultwarden_config() -> dict[str, object]:
    """Vaultwarden's config.json, generated from the VAULTWARDEN_* variables."""
    domain = env("VAULTWARDEN_DOMAIN")
    if not domain:
        domain = f"https://{required('FLY_APP_NAME')}.fly.dev"
    smtp = flag("VAULTWARDEN_ENABLE_SMTP", False)
    yubico = flag("VAULTWARDEN_ENABLE_YUBICO", False)
    config: dict[str, object] = {
        "log_level": env("VAULTWARDEN_LOG_LEVEL", "info"),
        "log_timestamp_format": "%Y-%m-%d %H:%M:%S.%3f",
        "enable_db_wal": True,
        "attachments_folder": f"{S3_MOUNT}/attachments",
        "icon_cache_folder": f"{S3_MOUNT}/icon_cache",
        "sends_folder": f"{S3_MOUNT}/sends",
        "domain": domain,
        "sends_allowed": flag("VAULTWARDEN_SENDS_ALLOWED", True),
        "hibp_api_key": env("VAULTWARDEN_HIBP_API_KEY"),
        "incomplete_2fa_time_limit": 3,
        "disable_icon_download": False,
        "signups_allowed": flag("VAULTWARDEN_SIGNUPS_ALLOWED", True),
        "signups_verify": flag("VAULTWARDEN_SIGNUPS_VERIFY", False),
        "signups_verify_resend_time": integer(
            "VAULTWARDEN_SIGNUPS_VERIFY_RESEND_TIME", 3600
        ),
        "signups_verify_resend_limit": integer(
            "VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT", 6
        ),
        "invitations_allowed": flag("VAULTWARDEN_INVITATIONS_ALLOWED", True),
        "emergency_access_allowed": flag("VAULTWARDEN_EMERGENCY_ACCESS_ALLOWED", True),
        "email_change_allowed": flag("VAULTWARDEN_EMAIL_CHANGE_ALLOWED", True),
        "password_iterations": positive_int("VAULTWARDEN_PASSWORD_ITERATIONS", 600000),
        "password_hints_allowed": flag("VAULTWARDEN_PASSWORD_HINTS_ALLOWED", True),
        "show_password_hint": flag("VAULTWARDEN_SHOW_PASSWORD_HINT", False),
        "admin_token": required("VAULTWARDEN_ADMIN_TOKEN"),
        "invitation_org_name": env("VAULTWARDEN_INVITATION_ORG_NAME", "Vaultwarden"),
        "ip_header": env("VAULTWARDEN_IP_HEADER", "X-Real-IP"),
        "icon_redirect_code": 302,
        "icon_cache_ttl": 2592000,
        "icon_cache_negttl": 259200,
        "icon_download_timeout": 10,
        "icon_blacklist_non_global_ips": True,
        "disable_2fa_remember": flag("VAULTWARDEN_DISABLE_2FA_REMEMBER", False),
        "authenticator_disable_time_drift": False,
        "require_device_email": False,
        "reload_templates": False,
        "use_sendmail": flag("VAULTWARDEN_USE_SENDMAIL", False),
        "_enable_yubico": yubico,
        "_enable_duo": flag("VAULTWARDEN_ENABLE_DUO", False),
        "_enable_smtp": smtp,
        "_enable_email_2fa": flag("VAULTWARDEN_ENABLE_EMAIL_2FA", smtp),
    }
    if smtp:
        config |= {
            "smtp_host": required("VAULTWARDEN_SMTP_HOST"),
            "smtp_security": env("VAULTWARDEN_SMTP_SECURITY", "force_tls"),
            "smtp_port": positive_int("VAULTWARDEN_SMTP_PORT", 465),
            "smtp_from": required("VAULTWARDEN_SMTP_FROM"),
            "smtp_from_name": env("VAULTWARDEN_SMTP_FROM_NAME", "Vaultwarden"),
            "smtp_username": required("VAULTWARDEN_SMTP_USERNAME"),
            "smtp_password": required("VAULTWARDEN_SMTP_PASSWORD"),
            "smtp_timeout": 15,
            "smtp_embed_images": True,
            "smtp_accept_invalid_certs": False,
            "smtp_accept_invalid_hostnames": False,
            "email_token_size": 6,
            "email_expiration_time": 600,
            "email_attempts_limit": 3,
        }
    if env("VAULTWARDEN_PUSH_INSTALLATION_ID"):
        config |= {
            "push_installation_id": env("VAULTWARDEN_PUSH_INSTALLATION_ID"),
            "push_installation_key": required("VAULTWARDEN_PUSH_INSTALLATION_KEY"),
        }
    if yubico:
        config |= {
            "yubico_client_id": required("VAULTWARDEN_YUBICO_CLIENT_ID"),
            "yubico_secret_key": required("VAULTWARDEN_YUBICO_SECRET_KEY"),
        }
    config["admin_session_lifetime"] = 20
    return config


def write_config() -> None:
    path = DATA_DIR / "config.json"
    LOG.info("writing %s", path)
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(vaultwarden_config(), indent=2) + "\n")
    # Read-only, so that the admin panel only serves to view the settings.
    path.chmod(0o444)


def maybe_idle(reason: str) -> None:
    # Not validated: this also runs after a configuration error.
    if env("ENTRYPOINT_IDLE") == "true":
        LOG.info("ENTRYPOINT_IDLE=true, entering idle state %s", reason)
        while True:
            time.sleep(3600)


def litestream_config() -> str:
    secret = required("AGE_SECRET_KEY")
    LOG.info("$ age-keygen -y")
    recipient = subprocess.run(
        ["age-keygen", "-y"],
        input=secret + "\n",
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    endpoint = env("AWS_ENDPOINT_URL_S3")
    # JSON strings are valid YAML scalars. Without access keys in the configuration,
    # Litestream uses the AWS SDK's credential chain.
    lines = [
        "dbs:",
        f"- path: {json.dumps(str(DATA_DIR / 'db.sqlite3'))}",
        "  replicas:",
        "  - type: s3",
        f"    bucket: {json.dumps(required('BUCKET_NAME'))}",
        f"    path: {json.dumps(REPLICA_PATH)}",
        f"    region: {json.dumps(required('AWS_REGION'))}",
        *([f"    endpoint: {json.dumps(endpoint)}"] if endpoint else []),
        # See https://litestream.io/reference/config/#replica-settings
        f"    sync-interval: {json.dumps(env('LITESTREAM_SYNC_INTERVAL', '10s'))}",
        "    age:",
        f"      identities: [{json.dumps(secret)}]",
        f"      recipients: [{json.dumps(recipient)}]",
        f"    retention: {json.dumps(env('LITESTREAM_RETENTION', '24h'))}",
        "    retention-check-interval: "
        + json.dumps(env("LITESTREAM_RETENTION_CHECK_INTERVAL", "1h")),
        "    validation-interval: "
        + json.dumps(env("LITESTREAM_VALIDATION_INTERVAL", "12h")),
    ]
    return "\n".join(lines) + "\n"


def write_litestream_config() -> None:
    LOG.info("writing %s", LITESTREAM_CONFIG)
    LITESTREAM_CONFIG.unlink(missing_ok=True)
    LITESTREAM_CONFIG.touch(mode=0o600)
    LITESTREAM_CONFIG.write_text(litestream_config())


def s3_client() -> S3Client:
    # backup.py lives next to this file.
    import backup

    return backup.s3_store(
        bucket=required("BUCKET_NAME"),
        prefix="",
        region=required("AWS_REGION"),
        credentials=None,
        endpoint_url=env("AWS_ENDPOINT_URL_S3") or None,
    ).client


def import_database(key: str) -> None:
    """Load an SQLite database from the bucket instead of restoring the replica."""
    from botocore.exceptions import ClientError

    bucket = required("BUCKET_NAME")
    database = DATA_DIR / "db.sqlite3"
    LOG.info('importing database file "%s" from S3 bucket "%s"', key, bucket)
    LOG.info(
        "remember to unset the IMPORT_DATABASE variable once the import is complete"
    )
    try:
        response = s3_client().get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise StartupError(
                f'could not find file "{key}" in S3 bucket "{bucket}"'
            ) from None
        raise
    partial = database.with_name(database.name + ".import")
    with closing(response["Body"]) as body, partial.open("wb") as output:
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            output.write(chunk)
    partial.replace(database)


def prepare_database() -> list[str]:
    """Restore or import the database and return the command that runs Vaultwarden."""
    litestream = flag("LITESTREAM_ENABLED", True)
    if litestream:
        write_litestream_config()
    if key := env("IMPORT_DATABASE"):
        import_database(key)
    elif litestream:
        database = str(DATA_DIR / "db.sqlite3")
        run(
            ["litestream", "restore", "-config", str(LITESTREAM_CONFIG)]
            + ["-if-db-not-exists", "-if-replica-exists", "-replica", "s3", database]
        )
    if not litestream:
        LOG.warning("Litestream is disabled, the database is not persisted.")
        return [VAULTWARDEN]
    return [
        "litestream",
        "replicate",
        "-config",
        str(LITESTREAM_CONFIG),
        "-exec",
        VAULTWARDEN,
    ]


#
# S3 mount monitor
#


def process_names() -> set[str]:
    """Names of running processes. Zombies are left out: a crashed GeeseFS daemon is
    not reaped while the entrypoint runs as PID 1 in a container."""
    names = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                continue  # The process exited in the meantime.
            # Format: "pid (comm) state ...", where comm may contain spaces and parens.
            name, _, rest = stat.partition(" (")[2].rpartition(") ")
            if not rest.startswith("Z"):
                names.add(name)
    return names


class Health(IntEnum):
    OK = 0
    # Possibly transient; counts towards the failure threshold.
    DEGRADED = 1
    # The GeeseFS process is gone or the mount disappeared.
    BROKEN = 2


class Deadline:
    """Runs commands for at most a given time, without ever blocking on them.

    Unlike subprocess.run(timeout=...), this never waits for a process that is stuck in a
    request to a dead FUSE mount, which not even SIGKILL may be able to end. Such
    processes are reaped later if they ever exit.
    """

    def __init__(self) -> None:
        self.abandoned: list[subprocess.Popen[bytes]] = []

    def __call__(self, timeout: float, command: Sequence[str]) -> bool:
        self.abandoned = [p for p in self.abandoned if p.poll() is None]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                self.abandoned.append(process)
                return False
            time.sleep(0.1)
        return process.returncode == 0


class S3Monitor:
    """Terminates the application when /mnt/s3 looks unrecoverable, to restart the machine."""

    def __init__(self) -> None:
        self.interval = positive_int("GEESEFS_MONITOR_INTERVAL", 30)
        self.timeout = positive_int("GEESEFS_MONITOR_TIMEOUT", 20)
        self.threshold = positive_int("GEESEFS_MONITOR_FAILURE_THRESHOLD", 3)
        self.write_check = flag("GEESEFS_MONITOR_WRITE_CHECK", True)
        self.probe = (
            S3_MOUNT / f".s3-monitor-{env('FLY_MACHINE_ID') or os.uname().nodename}"
        )
        self.run_with_deadline = Deadline()
        self.stopped = threading.Event()
        self.failed = False

    def check_mount(self) -> Health:
        if "geesefs" not in process_names():
            LOG.error("s3-monitor: geesefs process is not running")
            return Health.BROKEN
        mounts = [
            line.split() for line in Path("/proc/mounts").read_text().splitlines()
        ]
        if not any(
            fields[1] == str(S3_MOUNT) and fields[2].startswith("fuse")
            for fields in mounts
        ):
            LOG.error("s3-monitor: %s is not mounted", S3_MOUNT)
            return Health.BROKEN
        # Listing goes to S3 once GeeseFS' stat cache expires, so this also catches a
        # broken connection.
        if not self.run_with_deadline(self.timeout, ["ls", str(S3_MOUNT)]):
            LOG.warning(
                "s3-monitor: listing %s failed or did not complete within %ss",
                S3_MOUNT,
                self.timeout,
            )
            return Health.DEGRADED
        # Write and fsync a small file, which makes GeeseFS upload it right away. This
        # catches a mount that still serves reads from its cache but can't write to S3.
        if self.write_check and not self.run_with_deadline(
            self.timeout,
            ["sh", "-c", 'date +%s | dd of="$1" conv=fsync', "_", str(self.probe)],
        ):
            LOG.warning(
                "s3-monitor: writing %s failed or did not complete within %ss",
                self.probe,
                self.timeout,
            )
            return Health.DEGRADED
        return Health.OK

    def check_bucket(self) -> bool:
        """Whether the bucket can be reached directly, without going through GeeseFS."""
        result: list[bool] = []

        def attempt() -> None:
            try:
                s3_client().list_objects_v2(Bucket=required("BUCKET_NAME"), MaxKeys=1)
                result.append(True)
            except Exception:
                result.append(False)

        # Requests time out on their own; the thread only bounds the total time.
        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(self.timeout)
        return result == [True]

    def run(self, main: subprocess.Popen[bytes]) -> None:
        LOG.info(
            "s3-monitor: started (interval %ss, timeout %ss, failure threshold %s, write check %s)",
            self.interval,
            self.timeout,
            self.threshold,
            self.write_check,
        )
        failures = 0
        while not self.stopped.wait(self.interval) and main.poll() is None:
            health = self.check_mount()
            if health == Health.OK:
                if failures:
                    LOG.info(
                        "s3-monitor: %s recovered after %s failed check(s)",
                        S3_MOUNT,
                        failures,
                    )
                failures = 0
                continue
            failures += 1
            if health == Health.DEGRADED and failures < self.threshold:
                LOG.warning(
                    "s3-monitor: check failed (%s/%s)", failures, self.threshold
                )
                continue
            # The disk does not survive a restart: Litestream must be able to upload its
            # pending changes on shutdown and restore the database on startup. During an
            # S3 outage, keep serving from the local database instead.
            if not self.check_bucket():
                LOG.warning(
                    "s3-monitor: %s is broken, but S3 itself is unreachable too; not "
                    "restarting while that is the case to avoid losing database changes "
                    "that Litestream has not uploaded yet",
                    S3_MOUNT,
                )
                continue
            if self.stopped.is_set():
                return
            LOG.error(
                "s3-monitor: %s looks unrecoverable, terminating to force a restart",
                S3_MOUNT,
            )
            self.failed = True
            main.terminate()
            # Give Litestream a chance to shut down and push its last frames.
            if not self.stopped.wait(RESTART_GRACE_SECONDS) and main.poll() is None:
                main.kill()
            return

    def stop(self) -> None:
        # A deliberate stop must not turn a check failing during shutdown into a restart.
        self.stopped.set()
        self.failed = False


def supervise(command: Sequence[str], monitor: S3Monitor | None) -> int:
    """Run the application, forward termination signals and supervise /mnt/s3."""
    LOG.info("$ %s", " ".join(command))
    main = subprocess.Popen(command)

    def stop(_signum: int, _frame: FrameType | None) -> None:
        if monitor is not None:
            monitor.stop()
        main.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if monitor is not None:
        threading.Thread(target=monitor.run, args=(main,), daemon=True).start()
    status = main.wait()
    if monitor is not None and monitor.failed:
        LOG.error("exiting because the S3 monitor detected a broken %s mount", S3_MOUNT)
        return 1
    return status if status >= 0 else 128 - status


def main() -> int:
    monitor = (
        S3Monitor()
        if flag("GEESEFS_ENABLED", True) and flag("GEESEFS_MONITOR_ENABLED", True)
        else None
    )
    backup = flag("BACKUP_ENABLED", False)
    mount_s3()
    write_rsa_key()
    write_config()
    maybe_idle("before starting the application")
    os.environ["I_REALLY_WANT_VOLATILE_STORAGE"] = "true"
    command = prepare_database()
    if backup:
        command = [sys.executable, str(BACKUP), "supervise", *command]
    return supervise(command, monitor)


def cli() -> int:
    logging.basicConfig(
        level=logging.INFO, format="[entrypoint | %(levelname)5s]: %(message)s"
    )
    logging.getLogger("botocore.credentials").setLevel(logging.WARNING)
    try:
        return main()
    except Exception as error:
        if isinstance(error, (StartupError, subprocess.CalledProcessError)):
            LOG.error("%s", error)
        else:
            LOG.exception("an unexpected error occurred")
        maybe_idle("after a startup failure")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
