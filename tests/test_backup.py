import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[1] / "vaultwarden-fly-io" / "backup.py"
spec = importlib.util.spec_from_file_location("backup", MODULE)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class MemoryS3:
    """In-memory object store exercising publication ordering and failure boundaries."""

    def __init__(self):
        self.data = {}
        self.metadata = {}
        self.puts = []
        self.fail_archive = False
        self.fail_listing = False

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        if self.fail_listing:
            raise OSError("source unavailable")
        # Multiple pages expose scheduling bugs that only inspect the first page.
        for key, data in list(self.data.items()):
            if key.startswith(Prefix):
                yield {
                    "Contents": [
                        {"Key": key, "Size": len(data), "ETag": self.etag(data)}
                    ]
                }

    @staticmethod
    def etag(data):
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    def get_object(self, Bucket, Key, IfMatch=None):
        data = self.data[Key]
        if IfMatch and IfMatch != self.etag(data):
            raise OSError("precondition failed")
        return {"Body": io.BytesIO(data)}

    def head_object(self, Bucket, Key):
        return {
            "ContentLength": len(self.data[Key]),
            "Metadata": self.metadata.get(Key, {}),
        }

    def put_object(self, Bucket, Key, Body, IfNoneMatch, **kwargs):
        if self.fail_archive and "/archives/" in Key:
            raise OSError("upload failed")
        assert IfNoneMatch == "*"
        if Key in self.data:
            raise OSError("precondition failed")
        data = Body.read() if hasattr(Body, "read") else Body
        if "ChecksumSHA256" in kwargs:
            assert (
                kwargs["ChecksumSHA256"]
                == base64.b64encode(hashlib.sha256(data).digest()).decode()
            )
        self.data[Key] = data
        self.metadata[Key] = kwargs.get("Metadata", {})
        self.puts.append(Key)


def initialize_database(path):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE users (uuid TEXT);
        CREATE TABLE ciphers (uuid TEXT);
        CREATE TABLE attachments (id TEXT, cipher_uuid TEXT, file_size INTEGER);
        CREATE TABLE sends (uuid TEXT, atype INTEGER, data TEXT);
        INSERT INTO users VALUES ('alice');
        INSERT INTO attachments VALUES ('file', 'cipher', 10);
        INSERT INTO sends VALUES ('send', 1, '{"Id":"file", "Size":"4"}');
    """)
    db.commit()
    return db


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.connection = initialize_database(self.data / "db.sqlite3")
        self.addCleanup(self.connection.close)
        (self.data / "config.json").write_text(
            json.dumps(
                {"domain": "https://vault.example", "smtp_password": "mail-secret"}
            )
        )
        (self.data / "rsa_key.pem").write_text("actual-signing-key")
        self.key = self.root / "recovery-key"
        subprocess.run(
            ["age-keygen", "-o", str(self.key)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        self.recipient = (
            subprocess.check_output(["age-keygen", "-y", str(self.key)], timeout=10)
            .decode()
            .strip()
        )
        self.source_client = MemoryS3()
        self.source_client.data = {
            "data/attachments/cipher/file": b"attachment",
            "data/sends/send/file": b"send",
        }
        self.destination_client = MemoryS3()
        self.source = backup.Store(self.source_client, "source", "data")
        self.destination = backup.Store(
            self.destination_client,
            "backups",
            "kalix.cluster.rosenstein.app/vaultwarden",
        )
        self.env = patch.dict(
            os.environ,
            {
                "PATH": os.environ["PATH"],
                "BACKUP_AGE_RECIPIENT": self.recipient,
                "BACKUP_INTERVAL_SECONDS": "3600",
                "AGE_SECRET_KEY": "litestream-secret",
                "UNRELATED_SECRET": "must-not-be-exported",
                "BACKUP_AWS_SECRET_ACCESS_KEY": "destination-secret",
                "VAULTWARDEN_SMTP_PASSWORD": "mail-secret",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_capture(self):
        work = self.root / str(time.time_ns())
        work.mkdir()
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(status=200))
        response.__exit__ = Mock(return_value=False)
        with (
            patch.object(backup, "DATA_DIR", self.data),
            patch.object(
                backup,
                "s3_store",
                side_effect=lambda backup=False: (
                    self.destination if backup else self.source
                ),
            ),
            patch.object(backup, "urlopen", return_value=response),
            patch.object(
                backup.subprocess, "check_output", return_value=b"Vaultwarden 1.37.2"
            ),
        ):
            return backup.run_once(work)

    def test_capture_encrypt_restore_and_restart_schedule(self):
        next_due = self.run_capture()
        keys = self.destination_client.puts.copy()
        self.assertEqual(len(keys), 2)
        self.assertIn("/archives/", keys[0])
        self.assertIn("/completed/", keys[1])
        completion = json.loads(self.destination_client.data[keys[1]])
        self.assertEqual(next_due, backup.parse_utc(completion["captured_at"]) + 3600)
        encrypted = self.root / "archive.age"
        encrypted.write_bytes(self.destination_client.data[keys[0]])
        decrypted = subprocess.check_output(
            ["age", "-d", "-i", str(self.key), str(encrypted)], timeout=10
        )
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
            environment = json.load(
                archive.extractfile("vaultwarden/recovery/environment.json")
            )
            self.assertEqual(environment["AGE_SECRET_KEY"], "litestream-secret")
            self.assertEqual(environment["VAULTWARDEN_DOMAIN"], "https://vault.example")
            self.assertEqual(
                environment["VAULTWARDEN_RSA_PRIVATE_KEY"], "actual-signing-key"
            )
            self.assertNotIn("UNRELATED_SECRET", environment)
            self.assertNotIn("BACKUP_AWS_SECRET_ACCESS_KEY", environment)
            restored = self.root / "restored.sqlite3"
            restored.write_bytes(archive.extractfile("vaultwarden/db.sqlite3").read())
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(
                    db.execute("SELECT uuid FROM users").fetchall(), [("alice",)]
                )
            manifest = json.load(archive.extractfile("vaultwarden/manifest.json"))
            for name, details in manifest["files"].items():
                content = archive.extractfile("vaultwarden/" + name).read()
                self.assertEqual(hashlib.sha256(content).hexdigest(), details["sha256"])
                self.assertEqual(len(content), details["size"])
        # A fresh local work directory still uses durable scheduling state.
        self.assertEqual(self.run_capture(), next_due)
        self.assertEqual(keys, self.destination_client.puts)

    def test_missing_attachment_never_publishes(self):
        del self.source_client.data["data/attachments/cipher/file"]
        with self.assertRaises(FileNotFoundError):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_truncated_attachment_never_publishes(self):
        self.source_client.data["data/attachments/cipher/file"] = b"short"
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_lowercase_send_data(self):
        self.connection.execute("UPDATE sends SET data=?", ('{"id":"file", "size":4}',))
        self.connection.commit()
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 2)

    def test_failed_upload_does_not_advance_schedule(self):
        self.destination_client.fail_archive = True
        with self.assertRaises(OSError):
            self.run_capture()
        self.assertIsNone(backup.latest_capture(self.destination, time.time()))
        self.destination_client.fail_archive = False
        self.run_capture()

    def test_listing_error_does_not_trigger_capture(self):
        self.destination_client.fail_listing = True
        with self.assertRaises(OSError):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_orphan_archive_does_not_count_as_success(self):
        self.destination_client.data[
            self.destination.key("archives/orphan.tar.age")
        ] = b"partial"
        self.assertIsNone(backup.latest_capture(self.destination, time.time()))
        self.run_capture()

    def test_archive_missing_after_completion_is_error(self):
        self.run_capture()
        del self.destination_client.data[self.destination_client.puts[0]]
        with self.assertRaises(KeyError):
            self.run_capture()

    def test_size_limit(self):
        with patch.dict(os.environ, {"BACKUP_MAX_BYTES": "1"}):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_rejects_object_path_traversal(self):
        self.source_client.data["data/attachments/../../outside"] = b"bad"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.run_capture()
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual(self.destination_client.puts, [])

    def test_database_does_not_create_missing_source(self):
        missing = self.root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            backup.snapshot_database(missing, self.root / "out.db")
        self.assertFalse(missing.exists())

    def test_rejects_uninitialized_database(self):
        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        with self.assertRaisesRegex(ValueError, "initialized"):
            backup.snapshot_database(empty, self.root / "out.db")

    def test_destination_credentials_are_separate(self):
        env = {
            "AWS_ACCESS_KEY_ID": "source",
            "AWS_SECRET_ACCESS_KEY": "source-secret",
            "BACKUP_AWS_ACCESS_KEY_ID": "destination",
            "BACKUP_AWS_SECRET_ACCESS_KEY": "dest-secret",
            "BACKUP_AWS_REGION": "eu-west-1",
            "BACKUP_BUCKET_NAME": "backups",
            "BACKUP_PREFIX": "vaultwarden",
        }
        with (
            patch.dict(os.environ, env),
            patch.object(backup.boto3, "client") as client,
        ):
            backup.s3_store(backup=True)
        self.assertEqual(client.call_args.kwargs["aws_access_key_id"], "destination")
        self.assertEqual(
            client.call_args.kwargs["aws_secret_access_key"], "dest-secret"
        )

    def test_destination_does_not_inherit_source_endpoint(self):
        env = {
            "AWS_ENDPOINT_URL_S3": "https://source.invalid",
            "BACKUP_AWS_ACCESS_KEY_ID": "destination",
            "BACKUP_AWS_SECRET_ACCESS_KEY": "secret",
            "BACKUP_AWS_REGION": "eu-west-1",
            "BACKUP_BUCKET_NAME": "backups",
            "BACKUP_PREFIX": "vaultwarden",
        }
        with patch.dict(os.environ, env):
            store = backup.s3_store(backup=True)
        self.assertEqual(
            store.client.meta.endpoint_url, "https://s3.eu-west-1.amazonaws.com"
        )


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def start(self, worker, app):
        worker_path = self.root / "worker.py"
        worker_path.write_text(worker)
        script = (
            f"import sys; sys.path.insert(0, {str(MODULE.parent)!r}); import backup; "
            f"backup.__file__={str(worker_path)!r}; "
            f"sys.exit(backup.supervise([sys.executable, '-c', {app!r}]))"
        )
        env = dict(
            os.environ, BACKUP_TIMEOUT_SECONDS="1", BACKUP_TMP_DIR=str(self.root)
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_backup_timeout_does_not_kill_application(self):
        process = self.start(
            "import time; time.sleep(60)",
            "import time; time.sleep(2); raise SystemExit(7)",
        )
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 7)
        self.assertIn(b"timeout=True", stderr)
        self.assertEqual(list(self.root.glob("vaultwarden-backup-*")), [])

    def test_backup_failure_does_not_kill_application(self):
        process = self.start(
            "raise SystemExit(1)", "import time; time.sleep(1); raise SystemExit(0)"
        )
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_sigterm_reaches_application_and_cleans_capture(self):
        ready = self.root / "ready"
        stopped = self.root / "stopped"
        app = (
            "import signal,time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(stopped)!r}).touch(), exit(0))); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        process = self.start("import time; time.sleep(60)", app)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(stopped.exists())
        self.assertEqual(list(self.root.glob("vaultwarden-backup-*")), [])


class ConfigurationCoverageTests(unittest.TestCase):
    def test_every_documented_option_is_captured_or_explicitly_excluded(self):
        readme = (MODULE.parents[1] / "README.md").read_text()
        documented = set(
            re.findall(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", readme, re.MULTILINE)
        )
        captured = set(backup.RECOVERY_FIELDS)
        excluded = set(backup.RECOVERY_EXCLUSIONS)
        self.assertTrue(documented)
        self.assertEqual(
            documented - captured - excluded,
            set(),
            "Classify each README option in backup.py",
        )
        self.assertEqual(captured & excluded, set())
        self.assertTrue(
            all(backup.RECOVERY_EXCLUSIONS.values()), "Every exclusion needs a reason"
        )

    def test_entrypoint_recovery_inputs_are_classified(self):
        entrypoint = (MODULE.parent / "entrypoint.sh").read_text()
        options = set(
            re.findall(
                r"\b(?:VAULTWARDEN|LITESTREAM|GEESEFS|BACKUP)_[A-Z0-9_]+\b", entrypoint
            )
        )
        # These are internal paths, not operator inputs.
        options -= {"VAULTWARDEN_CONFIG_PATH", "LITESTREAM_DATABASE_PATH"}
        self.assertEqual(
            options - set(backup.RECOVERY_FIELDS) - set(backup.RECOVERY_EXCLUSIONS),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
