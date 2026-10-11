from __future__ import annotations

import base64
import hashlib
import importlib
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
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Self, TypedDict, cast
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

from botocore.exceptions import ClientError

MODULE = Path(__file__).resolve().parents[1] / "vaultwarden-fly-io" / "backup.py"
if TYPE_CHECKING:
    import backup
else:
    sys.path.insert(0, str(MODULE.parent))
    backup = importlib.import_module("backup")


class ObjectSummary(TypedDict):
    Key: str
    Size: int
    ETag: str


class ObjectPage(TypedDict):
    Contents: list[ObjectSummary]


class ObjectKey(TypedDict):
    Key: str


class KeyPage(TypedDict):
    Contents: list[ObjectKey]


class ObjectBody(TypedDict):
    Body: io.BytesIO


class ObjectHead(TypedDict):
    ContentLength: int
    Metadata: dict[str, str]


def archive_file(archive: tarfile.TarFile, name: str) -> IO[bytes]:
    stream = archive.extractfile(name)
    assert stream is not None
    return stream


class MemoryS3:
    """In-memory object store exercising publication ordering and failure boundaries."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.puts: list[str] = []
        self.fail_archive = False
        self.fail_listing = False

    def get_paginator(self, name: str) -> Self:
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str) -> Iterator[ObjectPage]:
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

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int) -> KeyPage:
        if self.fail_listing:
            raise OSError("listing unavailable")
        keys: list[ObjectKey] = [
            {"Key": key} for key in sorted(self.data) if key.startswith(Prefix)
        ]
        return {"Contents": keys[:MaxKeys]}

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    def get_object(
        self, Bucket: str, Key: str, IfMatch: str | None = None
    ) -> ObjectBody:
        if Key not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.data[Key]
        if IfMatch and IfMatch != self.etag(data):
            raise OSError("precondition failed")
        return {"Body": io.BytesIO(data)}

    def head_object(self, Bucket: str, Key: str) -> ObjectHead:
        if Key not in self.data:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {
            "ContentLength": len(self.data[Key]),
            "Metadata": self.metadata.get(Key, {}),
        }

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: bytes | BinaryIO,
        IfNoneMatch: str,
        *,
        ContentLength: int | None = None,
        ContentType: str,
        ChecksumAlgorithm: str,
        ChecksumSHA256: str,
        Metadata: dict[str, str] | None = None,
    ) -> None:
        if self.fail_archive and "/archives/" in Key:
            raise OSError("upload failed")
        assert IfNoneMatch == "*"
        if Key in self.data:
            raise OSError("precondition failed")
        data = Body if isinstance(Body, bytes) else Body.read()
        assert (
            ChecksumSHA256 == base64.b64encode(hashlib.sha256(data).digest()).decode()
        )
        self.data[Key] = data
        self.metadata[Key] = Metadata or {}
        self.puts.append(Key)


def initialize_database(path: Path) -> sqlite3.Connection:
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
    def setUp(self) -> None:
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
        self.source = backup.Store(
            cast("S3Client", self.source_client), "source", "data"
        )
        self.destination = backup.Store(
            cast("S3Client", self.destination_client),
            "backups",
            "example.com/vaultwarden",
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
                "BACKUP_AWS_ACCESS_KEY_ID": "destination-key",
                "BACKUP_AWS_SESSION_TOKEN": "destination-session",
                "BACKUP_AWS_REGION": "eu-west-1",
                "BACKUP_BUCKET_NAME": "backups",
                "BACKUP_PREFIX": "example.com/vaultwarden",
                "BUCKET_NAME": "source",
                "AWS_ACCESS_KEY_ID": "source-key",
                "AWS_SECRET_ACCESS_KEY": "source-secret",
                "AWS_SESSION_TOKEN": "source-session",
                "AWS_REGION": "eu-central-1",
                "AWS_ENDPOINT_URL_S3": "https://source.invalid",
                "VAULTWARDEN_SMTP_PASSWORD": "mail-secret",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_capture(self) -> float:
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
                side_effect=lambda **config: (
                    self.destination if config["bucket"] == "backups" else self.source
                ),
            ) as stores,
            patch.object(backup, "urlopen", return_value=response),
            patch.object(
                subprocess, "check_output", return_value=b"Vaultwarden 1.37.2"
            ),
        ):
            next_due = backup.main(work)
            self.store_configs = [call.kwargs for call in stores.call_args_list]
            return next_due

    def test_capture_encrypt_restore_and_restart_schedule(self) -> None:
        next_due = self.run_capture()
        keys = self.destination_client.puts.copy()
        self.assertEqual(len(keys), 2)
        self.assertIn("/archives/", keys[0])
        self.assertIn("/completed/", keys[1])
        completion = backup.decode_completion(self.destination_client.data[keys[1]])
        self.assertEqual(completion["status"], "complete")
        self.assertEqual(completion["missing_file_count"], 0)
        self.assertEqual(next_due, backup.parse_utc(completion["captured_at"]) + 3600)
        encrypted = self.root / "archive.age"
        encrypted.write_bytes(self.destination_client.data[keys[0]])
        decrypted = subprocess.check_output(
            ["age", "-d", "-i", str(self.key), str(encrypted)], timeout=10
        )
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
            environment = json.load(
                archive_file(archive, "vaultwarden/recovery/environment.json")
            )
            self.assertEqual(environment["AGE_SECRET_KEY"], "litestream-secret")
            self.assertEqual(environment["VAULTWARDEN_DOMAIN"], "https://vault.example")
            self.assertEqual(
                environment["VAULTWARDEN_RSA_PRIVATE_KEY"], "actual-signing-key"
            )
            self.assertNotIn("UNRELATED_SECRET", environment)
            self.assertNotIn("BACKUP_AWS_SECRET_ACCESS_KEY", environment)
            restored = self.root / "restored.sqlite3"
            restored.write_bytes(archive_file(archive, "vaultwarden/db.sqlite3").read())
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(
                    db.execute("SELECT uuid FROM users").fetchall(), [("alice",)]
                )
            manifest = json.load(archive_file(archive, "vaultwarden/manifest.json"))
            for name, details in manifest["files"].items():
                content = archive_file(archive, "vaultwarden/" + name).read()
                self.assertEqual(hashlib.sha256(content).hexdigest(), details["sha256"])
                self.assertEqual(len(content), details["size"])
        # A fresh local work directory still uses durable scheduling state.
        self.assertEqual(self.run_capture(), next_due)
        self.assertEqual(keys, self.destination_client.puts)

    def test_missing_files_publish_degraded_recoverable_backup(self) -> None:
        for missing_key in ("data/attachments/cipher/file", "data/sends/send/file"):
            del self.source_client.data[missing_key]
        next_due = self.run_capture()
        keys = self.destination_client.puts.copy()
        completion = backup.decode_completion(self.destination_client.data[keys[1]])
        self.assertEqual(completion["status"], "degraded")
        self.assertEqual(completion["missing_file_count"], 2)
        raw_completion = self.destination_client.data[keys[1]]
        self.assertNotIn("missing_files", backup.json_object(raw_completion))
        self.assertNotIn(b"cipher/file", raw_completion)
        self.assertNotIn(b"send/file", raw_completion)
        encrypted = self.root / "archive.age"
        encrypted.write_bytes(self.destination_client.data[keys[0]])
        decrypted = subprocess.check_output(
            ["age", "-d", "-i", str(self.key), str(encrypted)], timeout=10
        )
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
            manifest = json.load(archive_file(archive, "vaultwarden/manifest.json"))
            self.assertEqual(
                manifest["missing_files"],
                [
                    {"path": "attachments/cipher/file", "expected_size": 10},
                    {"path": "sends/send/file", "expected_size": 4},
                ],
            )
            self.assertEqual(manifest["status"], "degraded")
            restored = self.root / "restored.db"
            restored.write_bytes(archive_file(archive, "vaultwarden/db.sqlite3").read())
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(
                    db.execute("SELECT uuid FROM users").fetchall(), [("alice",)]
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM attachments").fetchone(), (1,)
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM sends").fetchone(), (1,)
                )
            self.assertIn("vaultwarden/recovery/rsa_key.pem", archive.getnames())
        self.assertEqual(self.run_capture(), next_due)
        self.assertEqual(self.destination_client.puts, keys)

    def test_truncated_attachment_never_publishes(self) -> None:
        self.source_client.data["data/attachments/cipher/file"] = b"short"
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_lowercase_send_data(self) -> None:
        self.connection.execute("UPDATE sends SET data=?", ('{"id":"file", "size":4}',))
        self.connection.commit()
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 2)

    def test_numeric_send_size_is_preserved(self) -> None:
        self.connection.execute(
            "UPDATE sends SET data=?", ('{"Id":"file", "Size":4.0}',)
        )
        self.connection.commit()
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 2)

    def test_failed_upload_does_not_advance_schedule(self) -> None:
        self.destination_client.fail_archive = True
        with self.assertRaises(OSError):
            self.run_capture()
        self.assertIsNone(backup.latest_capture(self.destination, time.time()))
        self.destination_client.fail_archive = False
        self.run_capture()

    def test_listing_error_does_not_trigger_capture(self) -> None:
        self.destination_client.fail_listing = True
        with self.assertRaises(OSError):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_orphan_archive_does_not_count_as_success(self) -> None:
        self.destination_client.data[
            self.destination.key("archives/orphan.tar.age")
        ] = b"partial"
        self.assertIsNone(backup.latest_capture(self.destination, time.time()))
        self.run_capture()

    def test_archive_missing_after_completion_triggers_new_capture(self) -> None:
        self.run_capture()
        del self.destination_client.data[self.destination_client.puts[0]]
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 4)

    def test_invalid_newest_record_falls_back_to_valid_history(self) -> None:
        next_due = self.run_capture()
        key = self.destination_client.puts[1]
        valid = json.loads(self.destination_client.data[key])
        corruptions = [
            b"not json",
            b"x" * 65537,
            b"null",
            b"[]",
            b"{}",
            json.dumps(
                dict(valid, captured_at=backup.utc(time.time() + 3600))
            ).encode(),
            json.dumps(dict(valid, schema_version=999)).encode(),
            json.dumps(dict(valid, archive_key="outside/archive")).encode(),
            json.dumps(dict(valid, archive_size=999)).encode(),
        ]
        invalid_key = self.destination.key("completed/9999-invalid.json")
        for corruption in corruptions:
            with self.subTest(corruption=corruption[:100]):
                self.destination_client.data[invalid_key] = corruption
                self.assertEqual(self.run_capture(), next_due)
                self.assertEqual(len(self.destination_client.puts), 2)
        # If every record is unusable, create a fresh backup.
        del self.destination_client.data[key]
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 4)

    def test_disappearing_manifest_is_skipped(self) -> None:
        self.run_capture()
        with patch.object(
            self.destination_client,
            "get_object",
            side_effect=ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject"),
        ):
            self.assertIsNone(backup.latest_capture(self.destination, time.time()))

    def test_destination_read_errors_do_not_trigger_capture(self) -> None:
        self.run_capture()
        for method in ("get_object", "head_object"):
            for code in (
                "AccessDenied",
                "403",
                "InternalError",
                "SlowDown",
                "NoSuchBucket",
            ):
                with (
                    self.subTest(method=method, code=code),
                    patch.object(
                        self.destination_client,
                        method,
                        side_effect=ClientError({"Error": {"Code": code}}, method),
                    ),
                ):
                    with self.assertRaises(ClientError):
                        self.run_capture()
                    self.assertEqual(len(self.destination_client.puts), 2)

    def test_forbidden_archive_head_skips_only_confirmed_missing_archive(self) -> None:
        self.run_capture()
        archive_key = self.destination_client.puts[0]
        error = ClientError({"Error": {"Code": "403"}}, "HeadObject")
        with patch.object(self.destination_client, "head_object", side_effect=error):
            # An existing but unreadable archive is an access failure.
            with self.assertRaises(ClientError):
                backup.latest_capture(self.destination, time.time())
            # Failed listing cannot establish absence.
            with patch.object(
                self.destination_client,
                "list_objects_v2",
                side_effect=OSError("offline"),
            ):
                with self.assertRaises(OSError):
                    backup.latest_capture(self.destination, time.time())
            # Prefix siblings must not be confused with the exact archive key.
            del self.destination_client.data[archive_key]
            self.destination_client.data[archive_key + ".other"] = b"unrelated"
            with patch.object(
                self.destination_client,
                "list_objects_v2",
                wraps=self.destination_client.list_objects_v2,
            ) as listing:
                self.assertIsNone(backup.latest_capture(self.destination, time.time()))
                listing.assert_called_once_with(
                    Bucket=self.destination.bucket, Prefix=archive_key, MaxKeys=1
                )
        self.run_capture()
        self.assertEqual(len(self.destination_client.puts), 4)

    def test_source_download_failure_does_not_publish_degraded_backup(self) -> None:
        with patch.object(
            self.source_client, "get_object", side_effect=OSError("offline")
        ):
            with self.assertRaises(OSError):
                self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_size_limit(self) -> None:
        with patch.dict(os.environ, {"BACKUP_MAX_BYTES": "1"}):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_rejects_object_path_traversal(self) -> None:
        self.source_client.data["data/attachments/../../outside"] = b"bad"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.run_capture()
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual(self.destination_client.puts, [])

    def test_database_does_not_create_missing_source(self) -> None:
        missing = self.root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            backup.snapshot_database(missing, self.root / "out.db")
        self.assertFalse(missing.exists())

    def test_rejects_uninitialized_database(self) -> None:
        for table in ("users", "ciphers", "attachments", "sends"):
            self.connection.execute(f"DROP TABLE {table}")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "initialized"):
            self.run_capture()
        self.assertEqual(self.destination_client.puts, [])

    def test_destination_credentials_are_separate(self) -> None:
        self.run_capture()
        destination, source = self.store_configs
        self.assertEqual(
            destination,
            {
                "bucket": "backups",
                "prefix": "example.com/vaultwarden",
                "region": "eu-west-1",
                "credentials": backup.StaticCredentials(
                    "destination-key", "destination-secret", "destination-session"
                ),
                "endpoint_url": None,
            },
        )
        self.assertEqual(
            source,
            {
                "bucket": "source",
                "prefix": "data",
                "region": "eu-central-1",
                "credentials": None,
                "endpoint_url": "https://source.invalid",
            },
        )

    def test_web_identity_destination_credentials(self) -> None:
        for name in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
            del os.environ[f"BACKUP_AWS_{name}"]
        os.environ.update(
            BACKUP_AWS_ROLE_ARN="arn:aws:iam::123456789012:role/backup",
            BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE="/var/run/secrets/aws/token",
        )
        self.run_capture()
        destination, source = self.store_configs
        self.assertEqual(
            destination["credentials"],
            backup.WebIdentity(
                "arn:aws:iam::123456789012:role/backup",
                "/var/run/secrets/aws/token",
                "vaultwarden-backup",
            ),
        )
        self.assertIsNone(source["credentials"])

    def test_web_identity_rejects_static_credentials_and_partial_settings(
        self,
    ) -> None:
        os.environ["BACKUP_AWS_ROLE_ARN"] = "arn:aws:iam::123456789012:role/backup"
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_AWS_ACCESS_KEY_ID"):
            backup.destination_credentials()
        for name in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
            del os.environ[f"BACKUP_AWS_{name}"]
        with self.assertRaisesRegex(
            backup.BackupError, "BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE"
        ):
            backup.destination_credentials()
        del os.environ["BACKUP_AWS_ROLE_ARN"]
        os.environ["BACKUP_AWS_WEB_IDENTITY_TOKEN_FILE"] = "/var/run/secrets/aws/token"
        with self.assertRaisesRegex(backup.BackupError, "BACKUP_AWS_ROLE_ARN"):
            backup.destination_credentials()

    def test_source_uses_standard_credential_chain(self) -> None:
        store = backup.s3_store(
            bucket="source",
            prefix="data",
            region="eu-central-1",
            credentials=None,
            endpoint_url="https://source.invalid",
        )
        credentials = cast(Any, store.client)._request_signer._credentials
        self.assertEqual(credentials.method, "env")
        self.assertEqual(
            tuple(credentials.get_frozen_credentials()),
            ("source-key", "source-secret", "source-session", None),
        )

    def test_s3_factory_forwards_only_explicit_credentials(self) -> None:
        # Ambient source credentials in setUp must not replace destination inputs.
        for token in (None, "destination-token"):
            store = backup.s3_store(
                bucket="backups",
                prefix="vaultwarden",
                region="eu-west-1",
                credentials=backup.StaticCredentials("destination", "secret", token),
            )
            credentials = cast(Any, store.client)._request_signer._credentials
            self.assertEqual(
                tuple(credentials.get_frozen_credentials()),
                ("destination", "secret", token, None),
            )

    def test_web_identity_uses_only_configured_role_and_token(self) -> None:
        token = self.root / "token"
        token.write_text("jwt-1")
        # Ambient web identity, endpoint and static settings belong to other clients.
        os.environ.update(
            AWS_ROLE_ARN="arn:aws:iam::123456789012:role/ambient",
            AWS_WEB_IDENTITY_TOKEN_FILE="/nonexistent",
            AWS_ENDPOINT_URL_STS="https://sts.invalid",
        )
        calls: list[tuple[str | None, dict[str, object]]] = []

        def api_call(
            client: object, operation: str, params: dict[str, object]
        ) -> dict[str, object]:
            self.assertEqual(operation, "AssumeRoleWithWebIdentity")
            calls.append((cast(Any, client).meta.endpoint_url, params))
            return {
                "Credentials": {
                    "AccessKeyId": f"temporary-{len(calls)}",
                    "SecretAccessKey": "temporary-secret",
                    "SessionToken": "temporary-session",
                    # Inside botocore's advisory window: every access refreshes.
                    "Expiration": datetime.now(UTC) + timedelta(minutes=12),
                },
                "AssumedRoleUser": {
                    "Arn": "arn:aws:sts::123456789012:assumed-role/backup/x"
                },
            }

        store = backup.s3_store(
            bucket="backups",
            prefix="vaultwarden",
            region="eu-west-1",
            credentials=backup.WebIdentity(
                "arn:aws:iam::123456789012:role/backup", str(token), "session"
            ),
        )
        credentials = cast(Any, store.client)._request_signer._credentials
        self.assertEqual(credentials.method, "assume-role-with-web-identity")
        with patch("botocore.client.BaseClient._make_api_call", api_call):
            frozen = credentials.get_frozen_credentials()
            self.assertEqual(frozen.access_key, "temporary-1")
            # Rotated tokens are read again when the credentials are refreshed.
            token.write_text("jwt-2")
            self.assertEqual(
                credentials.get_frozen_credentials().access_key, "temporary-2"
            )
        self.assertEqual(
            calls,
            [
                (
                    "https://sts.eu-west-1.amazonaws.com",
                    {
                        "RoleArn": "arn:aws:iam::123456789012:role/backup",
                        "RoleSessionName": "session",
                        "WebIdentityToken": jwt,
                    },
                )
                for jwt in ("jwt-1", "jwt-2")
            ],
        )
        self.assertEqual(
            store.client.meta.endpoint_url, "https://s3.eu-west-1.amazonaws.com"
        )

    def test_destination_does_not_inherit_source_endpoint(self) -> None:
        store = backup.s3_store(
            bucket="backups",
            prefix="vaultwarden",
            region="eu-west-1",
            credentials=backup.StaticCredentials("destination", "secret"),
        )
        self.assertEqual(
            store.client.meta.endpoint_url, "https://s3.eu-west-1.amazonaws.com"
        )

    def test_file_capture_and_validation_accept_arbitrary_prefixes(self) -> None:
        self.source_client.data.update(
            {
                "data/documents/a": b"hello",
                "data/media/b": b"world",
                "data/private/c": b"excluded",
            }
        )
        files = self.root / "custom-files"
        prefixes = ("documents/", "media/")
        # The size budget is shared across prefixes.
        with self.assertRaisesRegex(ValueError, "exceeds"):
            backup.capture_files(
                self.source, self.root / "too-small", 9, prefixes=prefixes
            )
        backup.capture_files(self.source, files, 10, prefixes=prefixes)
        self.assertEqual(
            {str(p.relative_to(files)) for p in files.rglob("*") if p.is_file()},
            {"documents/a", "media/b"},
        )
        self.assertEqual(
            backup.validate_files(
                [("documents/a", 5), ("media/b", 5), ("documents/missing", 7)], files
            ),
            [{"path": "documents/missing", "expected_size": 7}],
        )
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            backup.validate_files([("documents/a", 6)], files)

    def test_snapshot_supports_other_sqlite_schemas(self) -> None:
        source, target = self.root / "generic.db", self.root / "snapshot.db"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE notes (text TEXT)")
            db.execute("INSERT INTO notes VALUES ('example')")
            db.commit()
        backup.snapshot_database(source, target)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(
                db.execute("SELECT text FROM notes").fetchall(), [("example",)]
            )

    def test_encryption_uses_explicit_archive_root(self) -> None:
        payload = self.root / "input"
        payload.mkdir()
        (payload / "file").write_bytes(b"contents")
        encrypted = self.root / "custom.age"
        backup.encrypt_archive(
            payload, encrypted, self.recipient, arcname="custom-root"
        )
        decrypted = subprocess.check_output(
            ["age", "-d", "-i", str(self.key), str(encrypted)], timeout=10
        )
        with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["custom-root", "custom-root/file"])
            self.assertEqual(
                archive_file(archive, "custom-root/file").read(), b"contents"
            )


class CompletionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record: backup.CompletionManifestV1 = {
            "schema_version": 1,
            "backup_id": "test-backup",
            "captured_at": "2026-10-10T01:00:00Z",
            "database_capture_finished_at": "2026-10-10T01:00:01Z",
            "completed_at": "2026-10-10T01:00:02Z",
            "vaultwarden_version": "Vaultwarden 1.37.4",
            "status": "complete",
            "missing_file_count": 0,
            "archive_key": "vaultwarden/archives/test-backup.tar.age",
            "archive_size": 1024,
            "archive_sha256": "a" * 64,
        }

    def test_version_one_complete_and_degraded_round_trip(self) -> None:
        self.assertEqual(
            backup.decode_completion(json.dumps(self.record).encode()), self.record
        )
        self.record["status"] = "degraded"
        self.record["missing_file_count"] = 2
        self.assertEqual(
            backup.decode_completion(json.dumps(self.record).encode()), self.record
        )

    def test_rejects_unknown_versions_and_invalid_field_types(self) -> None:
        invalid: list[tuple[str, object]] = [
            ("schema_version", 2),
            ("schema_version", "1"),
            ("schema_version", True),
            ("schema_version", 1.0),
            ("backup_id", None),
            ("captured_at", 123),
            ("database_capture_finished_at", "not-a-date"),
            ("completed_at", "2026-10-10T01:00:02"),
            ("vaultwarden_version", []),
            ("status", "success"),
            ("missing_file_count", True),
            ("missing_file_count", -1),
            ("missing_file_count", 1),
            ("archive_key", []),
            ("archive_size", 0),
            ("archive_size", "1024"),
            ("archive_size", True),
            ("archive_sha256", "bad"),
            ("archive_sha256", "a" * 64 + "\n"),
            ("archive_size", 1024.0),
        ]
        for key, value in invalid:
            with self.subTest(field=key, value=value):
                raw: dict[str, object] = dict(self.record)
                raw[key] = value
                with self.assertRaises(ValueError):
                    backup.decode_completion(json.dumps(raw).encode())
        self.record["status"] = "degraded"
        with self.assertRaises(backup.BackupError):
            backup.decode_completion(json.dumps(self.record).encode())

    def test_rejects_invalid_json_without_exposing_input(self) -> None:
        for raw in (b"[]", b"null", b"{", b'{"secret": "sensitive-value"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(backup.BackupError) as raised:
                    backup.decode_completion(raw)
                self.assertEqual(str(raised.exception), "invalid completion manifest")

    def test_rejects_each_missing_required_field(self) -> None:
        for key in self.record:
            with self.subTest(field=key):
                raw: dict[str, object] = dict(self.record)
                del raw[key]
                with self.assertRaises(backup.BackupError):
                    backup.decode_completion(json.dumps(raw).encode())


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def start(self, worker: str, app: str, setup: str = "") -> subprocess.Popen[bytes]:
        worker_path = self.root / "worker.py"
        worker_path.write_text(worker)
        script = (
            f"import sys; sys.path.insert(0, {str(MODULE.parent)!r}); import backup; "
            "assert 'boto3' not in sys.modules; "
            "import os; os.umask(0o022); "
            f"backup.__file__={str(worker_path)!r}; {setup} "
            f"sys.argv=['backup.py', 'supervise', sys.executable, '-c', {app!r}]; "
            "sys.exit(backup.cli())"
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

        def kill_if_running() -> None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

        self.addCleanup(kill_if_running)
        return process

    def test_backup_timeout_does_not_kill_application(self) -> None:
        process = self.start(
            "import time; time.sleep(60)",
            "import time; time.sleep(2); raise SystemExit(7)",
        )
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 7)
        self.assertIn(b"timeout=True", stderr)
        self.assertEqual(list(self.root.glob("vaultwarden-backup-*")), [])

    def test_backup_failure_does_not_kill_application(self) -> None:
        process = self.start(
            "raise SystemExit(1)", "import time; time.sleep(1); raise SystemExit(0)"
        )
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_cleanup_failure_does_not_stop_app_or_start_more_captures(self) -> None:
        starts = self.root / "starts"
        worker = f"from pathlib import Path; Path({str(starts)!r}).open('a').write('start\\n')"
        setup = "backup.tempfile.TemporaryDirectory.cleanup=lambda self: (_ for _ in ()).throw(OSError('failed'));"
        process = self.start(
            worker, "import time; time.sleep(2); raise SystemExit(7)", setup
        )
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 7)
        self.assertIn(b"cannot remove backup staging", stderr)
        self.assertEqual(starts.read_text(), "start\n")

    def test_unstoppable_worker_does_not_overlap(self) -> None:
        app = Mock(returncode=7)
        app.poll.side_effect = [None, None, None, 7, 7]
        capture = Mock()
        capture.poll.return_value = None
        staging = Mock(name="staging")
        staging.name = str(self.root)
        with (
            patch.object(signal, "signal"),
            patch.object(backup, "signal_group"),
            patch.object(subprocess, "Popen", side_effect=[app, capture]) as spawn,
            patch.object(tempfile, "TemporaryDirectory", return_value=staging),
            patch.object(backup, "stop_capture", return_value=False) as stop,
            patch.object(time, "monotonic", side_effect=[0, 2, 3]),
            patch.object(time, "sleep"),
            patch.dict(os.environ, {"BACKUP_TIMEOUT_SECONDS": "1"}),
        ):
            self.assertEqual(backup.supervise(["app"]), 7)
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(
            stop.call_count, 2
        )  # Timeout and final shutdown, not every poll.
        staging.cleanup.assert_not_called()

    def test_stop_capture_handles_wait_timeout(self) -> None:
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 10)
        with patch.object(backup, "signal_group"):
            self.assertFalse(backup.stop_capture(process))

    def test_supervisor_preserves_application_umask(self) -> None:
        output = self.root / "application-file"
        process = self.start(
            "raise SystemExit(1)",
            f"from pathlib import Path; Path({str(output)!r}).touch()",
        )
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_worker_uses_private_umask(self) -> None:
        observed: list[int] = []

        def observe_umask(_work: Path) -> float:
            observed.append(os.umask(0o077))
            return 123

        previous = os.umask(0o022)
        try:
            with (
                patch.object(sys, "argv", ["backup.py", "once", str(self.root)]),
                patch.object(
                    backup,
                    "main",
                    side_effect=observe_umask,
                ),
            ):
                self.assertEqual(backup.cli(), 0)
        finally:
            os.umask(previous)
        self.assertEqual(observed, [0o077])
        self.assertEqual((self.root / "schedule.json").stat().st_mode & 0o777, 0o600)

    def test_sigterm_reaches_application_and_cleans_capture(self) -> None:
        self.check_shutdown()
        self.assertEqual(list(self.root.glob("vaultwarden-backup-*")), [])

    def test_sigterm_reaches_application_even_if_staging_cleanup_fails(self) -> None:
        self.check_shutdown(
            "backup.tempfile.TemporaryDirectory.cleanup=lambda self: (_ for _ in ()).throw(OSError('failed'));"
        )

    def check_shutdown(self, setup: str = "") -> None:
        ready = self.root / "ready"
        stopped = self.root / "stopped"
        app = (
            "import signal,time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(stopped)!r}).touch(), exit(0))); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        process = self.start("import time; time.sleep(60)", app, setup)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(stopped.exists())


CONFIGURATION_REFERENCE = (
    MODULE.parents[1] / "docs/src/content/docs/reference/configuration.md"
)


def documented_options(page: str) -> set[str]:
    options = set()
    in_table = False
    for line in page.splitlines():
        if not line.strip().startswith("|"):
            in_table = False
            continue
        cell = line.split("|")[1].strip()
        if cell == "Variable":
            in_table = True
            continue
        if re.fullmatch(r":?-+:?", cell):
            continue
        if not in_table:
            if re.fullmatch(r"(?:`[A-Z][A-Z0-9_]*`|[A-Z][A-Z0-9_]*)", cell):
                raise ValueError("Configuration table must have a Variable header")
            continue
        if not re.fullmatch(r"(?:`[A-Z][A-Z0-9_]*`|[A-Z][A-Z0-9_]*)", cell):
            raise ValueError(f"Unrecognized configuration option: {cell!r}")
        options.add(cell.strip("`"))
    return options


class ConfigurationCoverageTests(unittest.TestCase):
    def test_every_documented_option_is_captured_or_explicitly_excluded(self) -> None:
        documented = documented_options(CONFIGURATION_REFERENCE.read_text())
        captured = set(backup.RECOVERY_FIELDS)
        excluded = set(backup.RECOVERY_EXCLUSIONS)
        self.assertTrue(documented)
        self.assertEqual(
            documented - captured - excluded,
            set(),
            "Classify each documented option in backup.py",
        )
        self.assertEqual(captured & excluded, set())
        self.assertTrue(
            all(backup.RECOVERY_EXCLUSIONS.values()), "Every exclusion needs a reason"
        )

    def test_documentation_parser_accepts_plain_names_and_rejects_ambiguous_rows(
        self,
    ) -> None:
        header = "| Variable | Default | Description |\n| --- | --- | --- |\n"
        self.assertEqual(
            documented_options(header + "| PLAIN_NAME | | |\n| `QUOTED_NAME` | | |"),
            {"PLAIN_NAME", "QUOTED_NAME"},
        )
        for row in ("**UNCLASSIFIED**", "FOO / BAR", "`UNMATCHED", ""):
            with self.subTest(row=row), self.assertRaises(ValueError):
                documented_options(header + f"| {row} | | |")

    def test_documentation_parser_rejects_unrecognized_table_headers(self) -> None:
        for header in ("Environment variable", "Option", "**Variable**"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                documented_options(
                    f"| {header} | Default |\n| --- | --- |\n| NEW_OPTION | |"
                )

    def test_entrypoint_recovery_inputs_are_classified(self) -> None:
        entrypoint = (MODULE.parent / "entrypoint.sh").read_text()
        options = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", entrypoint))
        options.update(re.findall(r"assert_is_set\s+([A-Z][A-Z0-9_]*)", entrypoint))
        # The entrypoint owns these paths rather than accepting them as inputs.
        options -= {
            "VAULTWARDEN_CONFIG_PATH",
            "LITESTREAM_DATABASE_PATH",
            "S3_MONITOR_FAILED_MARKER",
        }
        self.assertEqual(
            options - set(backup.RECOVERY_FIELDS) - set(backup.RECOVERY_EXCLUSIONS),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
