from __future__ import annotations

import importlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

MODULE = Path(__file__).resolve().parents[1] / "vaultwarden-fly-io" / "entrypoint.py"
if TYPE_CHECKING:
    import entrypoint
else:
    sys.path.insert(0, str(MODULE.parent))
    entrypoint = importlib.import_module("entrypoint")


class EntrypointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name, value in {
            "DATA_DIR": self.data,
            "S3_MOUNT": self.root / "s3",
            "LITESTREAM_CONFIG": self.root / "litestream.yml",
        }.items():
            patcher = patch.object(entrypoint, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(
            os.environ,
            {
                "PATH": os.environ["PATH"],
                "VAULTWARDEN_ADMIN_TOKEN": "admin",
                "FLY_APP_NAME": "app",
                "BUCKET_NAME": "bucket",
                "AWS_REGION": "eu-central-1",
                "AWS_ENDPOINT_URL_S3": "https://s3.example.com",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)


class ConfigTests(EntrypointTestCase):
    def test_defaults(self) -> None:
        entrypoint.write_config()
        path = self.data / "config.json"
        config = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(config["domain"], "https://app.fly.dev")
        self.assertEqual(config["admin_token"], "admin")
        self.assertIs(config["signups_allowed"], True)
        self.assertIs(config["_enable_email_2fa"], False)
        self.assertEqual(config["password_iterations"], 600000)
        self.assertEqual(
            config["attachments_folder"], f"{entrypoint.S3_MOUNT}/attachments"
        )
        self.assertNotIn("smtp_host", config)
        self.assertEqual(list(config)[-1], "admin_session_lifetime")
        # A restart rewrites the read-only file.
        os.environ["VAULTWARDEN_DOMAIN"] = "https://vault.example.com"
        entrypoint.write_config()
        self.assertEqual(
            json.loads(path.read_text())["domain"], "https://vault.example.com"
        )

    def test_typed_and_escaped_values(self) -> None:
        os.environ.update(
            VAULTWARDEN_SIGNUPS_ALLOWED="false",
            VAULTWARDEN_PASSWORD_ITERATIONS="700000",
            VAULTWARDEN_INVITATION_ORG_NAME='Quote " and \\ backslash',
            VAULTWARDEN_ENABLE_SMTP="true",
            VAULTWARDEN_SMTP_HOST="smtp.example.com",
            VAULTWARDEN_SMTP_FROM="vault@example.com",
            VAULTWARDEN_SMTP_USERNAME="user",
            VAULTWARDEN_SMTP_PASSWORD="pass",
            VAULTWARDEN_SMTP_PORT="587",
        )
        config = entrypoint.vaultwarden_config()
        self.assertIs(config["signups_allowed"], False)
        self.assertEqual(config["password_iterations"], 700000)
        self.assertEqual(config["invitation_org_name"], 'Quote " and \\ backslash')
        self.assertEqual(config["smtp_port"], 587)
        # Email 2FA follows SMTP unless set explicitly.
        self.assertIs(config["_enable_email_2fa"], True)
        os.environ["VAULTWARDEN_ENABLE_EMAIL_2FA"] = "false"
        self.assertIs(entrypoint.vaultwarden_config()["_enable_email_2fa"], False)

    def test_invalid_values_are_rejected(self) -> None:
        cases = {
            "VAULTWARDEN_SIGNUPS_ALLOWED": "yes",
            "VAULTWARDEN_PASSWORD_ITERATIONS": "0",
            "VAULTWARDEN_SIGNUPS_VERIFY_RESEND_LIMIT": "-1",
        }
        for name, value in cases.items():
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(entrypoint.StartupError, name):
                    entrypoint.vaultwarden_config()

    def test_required_values(self) -> None:
        cases: dict[str, dict[str, str]] = {
            "VAULTWARDEN_ADMIN_TOKEN": {"VAULTWARDEN_ADMIN_TOKEN": ""},
            "FLY_APP_NAME": {"FLY_APP_NAME": ""},
            "VAULTWARDEN_SMTP_HOST": {"VAULTWARDEN_ENABLE_SMTP": "true"},
            "VAULTWARDEN_PUSH_INSTALLATION_KEY": {
                "VAULTWARDEN_PUSH_INSTALLATION_ID": "id"
            },
            "VAULTWARDEN_YUBICO_CLIENT_ID": {"VAULTWARDEN_ENABLE_YUBICO": "true"},
        }
        for name, variables in cases.items():
            with self.subTest(name=name), patch.dict(os.environ, variables):
                with self.assertRaisesRegex(entrypoint.StartupError, name):
                    entrypoint.vaultwarden_config()

    def test_rsa_key(self) -> None:
        key = subprocess.run(
            ["openssl", "genrsa", "2048"], check=True, capture_output=True, text=True
        ).stdout.strip()
        os.environ["VAULTWARDEN_RSA_PRIVATE_KEY"] = key
        entrypoint.write_rsa_key()
        self.assertEqual((self.data / "rsa_key.pem").read_text(), key + "\n")
        self.assertIn("BEGIN PUBLIC KEY", (self.data / "rsa_key.pub.pem").read_text())


class LitestreamTests(EntrypointTestCase):
    def setUp(self) -> None:
        super().setUp()
        output = subprocess.run(
            ["age-keygen"], check=True, capture_output=True, text=True
        ).stdout
        self.secret = next(
            line for line in output.splitlines() if line.startswith("AGE-SECRET-KEY")
        )
        self.recipient = subprocess.run(
            ["age-keygen", "-y"],
            input=self.secret,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        os.environ["AGE_SECRET_KEY"] = self.secret

    def test_config_without_credentials(self) -> None:
        os.environ["AWS_ACCESS_KEY_ID"] = "must-not-be-written"
        os.environ["LITESTREAM_SYNC_INTERVAL"] = "1s"
        database = json.dumps(str(self.data / "db.sqlite3"))
        self.assertEqual(
            entrypoint.litestream_config(),
            textwrap.dedent(f"""\
                dbs:
                - path: {database}
                  replicas:
                  - type: s3
                    bucket: "bucket"
                    path: "vaultwarden.db"
                    region: "eu-central-1"
                    endpoint: "https://s3.example.com"
                    sync-interval: "1s"
                    age:
                      identities: ["{self.secret}"]
                      recipients: ["{self.recipient}"]
                    retention: "24h"
                    retention-check-interval: "1h"
                    validation-interval: "12h"
                """),
        )

    def test_config_without_endpoint_uses_aws(self) -> None:
        del os.environ["AWS_ENDPOINT_URL_S3"]
        self.assertNotIn("endpoint", entrypoint.litestream_config())

    def test_config_is_private(self) -> None:
        entrypoint.write_litestream_config()
        self.assertEqual(entrypoint.LITESTREAM_CONFIG.stat().st_mode & 0o777, 0o600)

    def test_restore_and_replicate(self) -> None:
        with patch.object(entrypoint, "run") as run:
            command = entrypoint.prepare_database()
        config = str(entrypoint.LITESTREAM_CONFIG)
        run.assert_called_once_with(
            ["litestream", "restore", "-config", config, "-if-db-not-exists"]
            + ["-if-replica-exists", "-replica", "s3", str(self.data / "db.sqlite3")]
        )
        self.assertEqual(
            command,
            ["litestream", "replicate", "-config", config, "-exec", "/vaultwarden"],
        )

    def test_disabled(self) -> None:
        os.environ["LITESTREAM_ENABLED"] = "false"
        with patch.object(entrypoint, "run") as run:
            self.assertEqual(entrypoint.prepare_database(), ["/vaultwarden"])
        run.assert_not_called()
        self.assertFalse(entrypoint.LITESTREAM_CONFIG.exists())

    def test_import_replaces_restore(self) -> None:
        os.environ["IMPORT_DATABASE"] = "import-db.sqlite"
        client = FakeS3({("bucket", "import-db.sqlite"): b"database"})
        with (
            patch.object(entrypoint, "run") as run,
            patch.object(entrypoint, "s3_client", return_value=client),
        ):
            command = entrypoint.prepare_database()
        run.assert_not_called()
        self.assertEqual((self.data / "db.sqlite3").read_bytes(), b"database")
        self.assertEqual(command[:2], ["litestream", "replicate"])

    def test_import_of_missing_object_fails(self) -> None:
        os.environ["IMPORT_DATABASE"] = "missing.sqlite"
        with (
            patch.object(entrypoint, "s3_client", return_value=FakeS3({})),
            self.assertRaisesRegex(entrypoint.StartupError, "missing.sqlite"),
        ):
            entrypoint.prepare_database()
        self.assertFalse((self.data / "db.sqlite3").exists())


class FakeS3:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}


class TokenFileTests(EntrypointTestCase):
    def test_waits_for_token_file(self) -> None:
        token = self.root / "token"
        os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"] = str(token)
        threading.Timer(0.3, token.write_text, ("jwt",)).start()
        entrypoint.wait_for_token_file()
        self.assertTrue(token.exists())

    def test_missing_token_file_fails(self) -> None:
        os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"] = str(self.root / "missing")
        with (
            patch.object(entrypoint, "TOKEN_FILE_TIMEOUT_SECONDS", 0.2),
            self.assertRaisesRegex(entrypoint.StartupError, "missing"),
        ):
            entrypoint.wait_for_token_file()

    def test_without_web_identity(self) -> None:
        entrypoint.wait_for_token_file()


class MountTests(EntrypointTestCase):
    def test_custom_endpoint(self) -> None:
        with patch.object(entrypoint, "run") as run:
            entrypoint.mount_s3()
        run.assert_called_once_with(
            ["geesefs", "--memory-limit", "64", "--endpoint", "https://s3.example.com"]
            + ["bucket:data/", str(entrypoint.S3_MOUNT)]
        )
        self.assertTrue(entrypoint.S3_MOUNT.is_dir())

    def test_aws_endpoint_is_derived_from_region(self) -> None:
        del os.environ["AWS_ENDPOINT_URL_S3"]
        with patch.object(entrypoint, "run") as run:
            entrypoint.mount_s3()
        self.assertEqual(
            run.call_args.args[0][3:7],
            [
                "--region",
                "eu-central-1",
                "--endpoint",
                "https://s3.eu-central-1.amazonaws.com",
            ],
        )

    def test_disabled(self) -> None:
        os.environ["GEESEFS_ENABLED"] = "false"
        with patch.object(entrypoint, "run") as run:
            entrypoint.mount_s3()
        run.assert_not_called()


class MonitorTests(EntrypointTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.update(
            GEESEFS_MONITOR_INTERVAL="1",
            GEESEFS_MONITOR_TIMEOUT="1",
            GEESEFS_MONITOR_FAILURE_THRESHOLD="2",
        )
        self.main = subprocess.Popen(["sleep", "60"])
        self.addCleanup(self.main.wait)
        self.addCleanup(self.main.kill)

    def monitor(
        self, health: list[entrypoint.Health], *, bucket: bool = True
    ) -> tuple[entrypoint.S3Monitor, Mock]:
        monitor = entrypoint.S3Monitor()
        checks = iter(health)
        check_bucket = Mock(return_value=bucket)
        for name, value in {
            "check_mount": Mock(side_effect=lambda: next(checks, health[-1])),
            "check_bucket": check_bucket,
        }.items():
            patcher = patch.object(monitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return monitor, check_bucket

    def run_for(self, monitor: entrypoint.S3Monitor, seconds: float) -> None:
        thread = threading.Thread(target=monitor.run, args=(self.main,))
        thread.start()
        time.sleep(seconds)
        monitor.stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_deadline_never_blocks_on_hung_commands(self) -> None:
        deadline = entrypoint.Deadline()
        self.assertTrue(deadline(5, ["true"]))
        self.assertFalse(deadline(5, ["false"]))
        started = time.monotonic()
        self.assertFalse(deadline(0.2, ["sleep", "10"]))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(deadline.abandoned), 1)
        deadline.abandoned[0].wait()

    def test_process_names_skip_zombies(self) -> None:
        # The process name comes from the executable's file name.
        name = "zz-monitor-test"
        executable = self.root / name
        executable.symlink_to(
            subprocess.check_output(["which", "sleep"], text=True).strip()
        )
        running = subprocess.Popen([executable, "60"])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        self.assertIn(name, entrypoint.process_names())
        running.kill()
        while Path(f"/proc/{running.pid}/stat").read_text().split(") ")[1][0] != "Z":
            time.sleep(0.01)
        self.assertNotIn(name, entrypoint.process_names())

    def test_broken_mount_terminates_application(self) -> None:
        monitor, _ = self.monitor([entrypoint.Health.BROKEN])
        with patch.object(entrypoint, "RESTART_GRACE_SECONDS", 1):
            monitor.run(self.main)
        self.assertTrue(monitor.failed)
        self.assertEqual(self.main.wait(timeout=5), -signal.SIGTERM)

    def test_transient_failures_below_threshold_recover(self) -> None:
        health = entrypoint.Health
        monitor, check_bucket = self.monitor(
            [health.DEGRADED, health.OK, health.DEGRADED, health.OK]
        )
        self.run_for(monitor, 4.5)
        check_bucket.assert_not_called()
        self.assertFalse(monitor.failed)
        self.assertIsNone(self.main.poll())

    def test_repeated_failures_reach_threshold(self) -> None:
        monitor, check_bucket = self.monitor([entrypoint.Health.DEGRADED])
        with patch.object(entrypoint, "RESTART_GRACE_SECONDS", 1):
            monitor.run(self.main)
        check_bucket.assert_called_once()
        self.assertTrue(monitor.failed)

    def test_s3_outage_does_not_restart(self) -> None:
        monitor, check_bucket = self.monitor([entrypoint.Health.BROKEN], bucket=False)
        self.run_for(monitor, 2.5)
        self.assertGreaterEqual(check_bucket.call_count, 2)
        self.assertFalse(monitor.failed)
        self.assertIsNone(self.main.poll())


class SuperviseTests(EntrypointTestCase):
    def supervise(self, script: str) -> subprocess.Popen[str]:
        code = (
            f"import sys; sys.path.insert(0, {str(MODULE.parent)!r}); import entrypoint;"
            f"sys.exit(entrypoint.supervise(['sh', '-c', {script!r}], None))"
        )
        return subprocess.Popen([sys.executable, "-c", code], text=True)

    def test_exit_status(self) -> None:
        self.assertEqual(self.supervise("exit 3").wait(timeout=10), 3)
        self.assertEqual(
            self.supervise("kill -KILL $$").wait(timeout=10), 128 + signal.SIGKILL
        )

    def test_stop_signals_are_forwarded(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                marker = self.root / f"stopped-{signum.name}"
                process = self.supervise(
                    f"trap 'touch {marker}; exit 7' TERM; touch {marker}.ready;"
                    "while :; do sleep 0.1; done"
                )
                ready = Path(f"{marker}.ready")
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                process.send_signal(signum)
                self.assertEqual(process.wait(timeout=10), 7)
                self.assertTrue(marker.exists())

    def test_monitor_failure_exits_with_error(self) -> None:
        monitor = entrypoint.S3Monitor()
        monitor.failed = True
        handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, handlers[0])
        self.addCleanup(signal.signal, signal.SIGINT, handlers[1])
        with patch.object(monitor, "run"):
            self.assertEqual(entrypoint.supervise(["true"], monitor), 1)


class StartupTests(EntrypointTestCase):
    """Runs the whole startup with stand-ins for GeeseFS, Litestream and Vaultwarden."""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.stub("geesefs", 'echo "geesefs $*" >> "$CALLS"')
        self.stub(
            "litestream",
            'echo "litestream $*" >> "$CALLS"\n'
            'if [ "$1" = replicate ]; then shift 3; exec "$2"; fi',
        )
        vaultwarden = self.stub(
            "vaultwarden",
            'echo "vaultwarden I_REALLY_WANT_VOLATILE_STORAGE=$I_REALLY_WANT_VOLATILE_STORAGE"'
            ' >> "$CALLS"',
        )
        key = subprocess.run(
            ["openssl", "genrsa", "2048"], check=True, capture_output=True, text=True
        ).stdout
        output = subprocess.run(
            ["age-keygen"], check=True, capture_output=True, text=True
        ).stdout
        os.environ.update(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            CALLS=str(self.calls),
            VAULTWARDEN_RSA_PRIVATE_KEY=key,
            AGE_SECRET_KEY=output.splitlines()[-1],
            GEESEFS_MONITOR_ENABLED="false",
        )
        patcher = patch.object(entrypoint, "VAULTWARDEN", str(vaultwarden))
        patcher.start()
        self.addCleanup(patcher.stop)
        handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, handlers[0])
        self.addCleanup(signal.signal, signal.SIGINT, handlers[1])
        self.addCleanup(os.environ.pop, "I_REALLY_WANT_VOLATILE_STORAGE", None)

    def stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return path

    def test_startup_sequence(self) -> None:
        self.assertEqual(entrypoint.cli(), 0)
        calls = self.calls.read_text().splitlines()
        config = entrypoint.LITESTREAM_CONFIG
        self.assertEqual(
            [call.split()[0:2] for call in calls],
            [
                ["geesefs", "--memory-limit"],
                ["litestream", "restore"],
                ["litestream", "replicate"],
                ["vaultwarden", "I_REALLY_WANT_VOLATILE_STORAGE=true"],
            ],
        )
        self.assertIn(f"-config {config} -exec {entrypoint.VAULTWARDEN}", calls[2])
        self.assertTrue((self.data / "config.json").exists())
        self.assertTrue((self.data / "rsa_key.pub.pem").exists())

    def test_startup_failure_exits_before_starting_anything(self) -> None:
        del os.environ["AGE_SECRET_KEY"]
        with self.assertLogs("entrypoint", "ERROR") as logs:
            self.assertEqual(entrypoint.cli(), 1)
        self.assertIn('"AGE_SECRET_KEY"', logs.output[-1])
        self.assertNotIn("litestream", self.calls.read_text())


if __name__ == "__main__":
    unittest.main()
