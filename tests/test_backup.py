from __future__ import annotations

import os
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import psycopg

import db
from backup import service


class BackupCryptoTests(unittest.TestCase):
    def test_settings_reject_missing_or_malformed_encryption_key(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user@localhost/radar",
                                     "BACKUP_ENCRYPTION_KEY": "bad key!"}):
            with self.assertRaisesRegex(ValueError, "encryption key"):
                service.Settings.from_env()

        import base64
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user@localhost/radar",
                                     "BACKUP_ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(31)).decode()}):
            with self.assertRaisesRegex(ValueError, "32 bytes"):
                service.Settings.from_env()

    def test_encrypted_stream_authenticates_payload(self):
        import hashlib
        import io

        key = bytes(range(32))
        output = io.BytesIO()
        service._encrypted_stream(io.BytesIO(b"test database dump"), output, key, hashlib.sha256())
        with tempfile.TemporaryDirectory() as temp_dir:
            encrypted = Path(temp_dir) / "copy.backup"
            plain = Path(temp_dir) / "restore.dump"
            encrypted.write_bytes(output.getvalue())
            service._decrypt_to_file(encrypted, plain, key)
            self.assertEqual(plain.read_bytes(), b"test database dump")
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                service._decrypt_to_file(encrypted, plain, bytes(reversed(range(32))))
            encrypted.write_bytes(output.getvalue()[:-1] + bytes([output.getvalue()[-1] ^ 1]))
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                service._decrypt_to_file(encrypted, plain, key)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class BackupPostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        configured_bin = os.environ.get("PG_TEST_BIN")
        if configured_bin:
            cls.pg_dump = Path(configured_bin) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
            cls.pg_restore = Path(configured_bin) / ("pg_restore.exe" if os.name == "nt" else "pg_restore")
        elif os.name == "nt":
            cls.pg_dump = Path(r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe")
            cls.pg_restore = Path(r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe")
        else:
            found_dump, found_restore = shutil.which("pg_dump"), shutil.which("pg_restore")
            if not found_dump or not found_restore:
                raise unittest.SkipTest("PostgreSQL client tools are not installed")
            cls.pg_dump, cls.pg_restore = Path(found_dump), Path(found_restore)
        cls.env = {"DATABASE_URL": cls.database_url,
                   "BACKUP_ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}
        cls.tool_patch = patch.dict(os.environ, {
            "PG_DUMP_PATH": str(cls.pg_dump),
            "PG_RESTORE_PATH": str(cls.pg_restore),
        })
        cls.tool_patch.start()
        with psycopg.connect(cls.database_url, autocommit=True) as conn:
            db.init_schema(conn)

    @classmethod
    def tearDownClass(cls):
        cls.tool_patch.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkingmp-backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.settings = service.Settings(self.database_url, Path(self.temp.name), bytes(range(32)))
        self.conn = psycopg.connect(self.database_url, autocommit=True)
        self.addCleanup(self.conn.close)

    def test_real_pg_dump_encrypts_and_restores_only_in_temporary_database(self):
        database_before = self.conn.execute("SELECT current_database()").fetchone()[0]
        temp_databases_before = self.conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'radar_backup_verify_%'"
        ).fetchone()[0]
        created = service.backup_once(self.conn, self.settings)
        self.assertTrue(created["file_name"].endswith(".backup"))
        content = (self.settings.backup_dir / created["file_name"]).read_bytes()
        self.assertTrue(content.startswith(service.MAGIC))
        self.assertNotIn(b"CREATE TABLE", content)
        verified = service.verify_latest(self.conn, self.settings)
        self.assertEqual(verified["status"], "success")
        self.assertEqual(self.conn.execute("SELECT current_database()").fetchone()[0], database_before)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'radar_backup_verify_%'"
        ).fetchone()[0], temp_databases_before)
        status = db.latest_backup_status(self.conn)
        self.assertEqual(status["last_status"], "success")
        self.assertEqual(status["last_restore_status"], "success")

    def test_checksum_failure_is_reported_without_attempting_restore(self):
        created = service.backup_once(self.conn, self.settings)
        path = self.settings.backup_dir / created["file_name"]
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            service.verify_latest(self.conn, self.settings)
        status = db.latest_backup_status(self.conn)
        self.assertEqual(status["last_restore_status"], "failed")

    def test_invalid_archive_marks_restore_failed_and_removes_temporary_database(self):
        created = service.backup_once(self.conn, self.settings)
        path = self.settings.backup_dir / created["file_name"]
        encrypted = io.BytesIO()
        digest = hashlib.sha256()
        size = service._encrypted_stream(io.BytesIO(b"not a PostgreSQL archive"), encrypted,
                                         self.settings.encryption_key, digest)
        path.write_bytes(encrypted.getvalue())
        self.conn.execute("UPDATE backup_runs SET size_bytes=%s, sha256=%s WHERE id=%s",
                          (size, digest.hexdigest(), created["id"]))
        before = self.conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'radar_backup_verify_%'"
        ).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "pg_restore_failed"):
            service.verify_latest(self.conn, self.settings)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'radar_backup_verify_%'"
        ).fetchone()[0], before)
        self.assertEqual(db.latest_backup_status(self.conn)["last_restore_status"], "failed")


class BackupFailureTests(unittest.TestCase):
    def test_restore_cleanup_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "copy.backup"
            content = b"encrypted backup"
            backup_path.write_bytes(content)
            settings = service.Settings("postgresql://user@localhost/radar", Path(temp_dir), bytes(range(32)))
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = (41, "copy.backup", len(content),
                                                               hashlib.sha256(content).hexdigest())

            created_admin = MagicMock()
            restored_db = MagicMock()
            restored_db.execute.side_effect = [
                MagicMock(fetchall=MagicMock(return_value=[(table,) for table in service.REQUIRED_TABLES])),
                MagicMock(fetchone=MagicMock(return_value=(1,))),
            ]
            cleanup_admin = MagicMock()
            cleanup_admin.execute.side_effect = RuntimeError("drop failed")

            create_context = MagicMock()
            create_context.__enter__.return_value = created_admin
            restored_context = MagicMock()
            restored_context.__enter__.return_value = restored_db
            cleanup_context = MagicMock()
            cleanup_context.__enter__.return_value = cleanup_admin

            with patch.object(service, "_checked_file", return_value=backup_path), \
                 patch.object(service, "_decrypt_to_file"), \
                 patch.object(service, "_tool", return_value="pg_restore"), \
                 patch.object(service.subprocess, "run", return_value=MagicMock(returncode=0)), \
                 patch.object(service.psycopg, "connect",
                              side_effect=[create_context, restored_context, cleanup_context]), \
                 patch.object(service.db, "mark_backup_restore") as mark:
                with self.assertRaisesRegex(RuntimeError, "verify_database_cleanup_failed"):
                    service.verify_latest(conn, settings, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

            self.assertEqual(mark.call_args.kwargs["status"], "failed")
            self.assertEqual(mark.call_args.kwargs["error_code"], "verify_database_cleanup_failed")

    def test_backup_commands_are_registered_without_entering_regular_cycle(self):
        import main

        self.assertIn("backup", main.COMMANDS)
        self.assertIn("verify-backup", main.COMMANDS)
        self.assertNotIn("backup", main.cycle.__code__.co_names)

    def test_retention_only_removes_generated_backup_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = service.Settings("postgresql://user@localhost/radar", root, bytes(range(32)), 2)
            for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
                (root / f"radar-{stamp}-0123456789abcdef.backup").write_bytes(b"x")
            (root / "user-backup.backup").write_bytes(b"keep")
            self.assertEqual(service._prune_backups(settings), 1)
            self.assertTrue((root / "radar-20260103T000000Z-0123456789abcdef.backup").exists())
            self.assertTrue((root / "radar-20260102T000000Z-0123456789abcdef.backup").exists())
            self.assertTrue((root / "user-backup.backup").exists())

    def test_failed_pg_dump_records_safe_error_code_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = service.Settings("postgresql://user@localhost/radar", Path(temp_dir), bytes(range(32)))
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = (41,)

            class FailedProcess:
                stdout = __import__("io").BytesIO(b"private dump contents")
                return_code = 1
                def wait(self):
                    return self.return_code

            with patch.object(service, "_tool", return_value="pg_dump"), \
                 patch.object(service.subprocess, "Popen", return_value=FailedProcess()), \
                 patch.object(service.db, "finish_backup_run") as finish:
                with self.assertRaisesRegex(RuntimeError, "pg_dump_failed"):
                    service.backup_once(conn, settings)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])
            self.assertEqual(finish.call_args.kwargs["error_code"], "pg_dump_failed")

    def test_existing_partial_collision_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = service.Settings("postgresql://user@localhost/radar", Path(temp_dir), bytes(range(32)))
            collision = Path(temp_dir) / "radar-20260101T000000Z-0123456789abcdef.backup.partial"
            collision.write_bytes(b"owner data")
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = (41,)
            with patch.object(service.secrets, "token_hex", return_value="0123456789abcdef"), \
                 patch.object(service.db, "finish_backup_run"):
                with self.assertRaises(RuntimeError):
                    service.backup_once(conn, settings, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(collision.read_bytes(), b"owner data")
