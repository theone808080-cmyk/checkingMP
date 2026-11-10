"""Encrypted, checksummed PostgreSQL backups and isolated restore checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from typing import BinaryIO

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from psycopg.conninfo import conninfo_to_dict
from psycopg import sql

import db

MAGIC = b"RDBAK1"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_SIZE = 1024 * 1024
BACKUP_NAME = re.compile(r"radar-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{16}\.backup\Z")
REQUIRED_TABLES = {"materials", "events", "event_materials", "cycles", "watchdog_incidents", "backup_runs"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    backup_dir: Path
    encryption_key: bytes
    retention_count: int = 14

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        encoded_key = os.environ.get("BACKUP_ENCRYPTION_KEY", "").strip()
        backup_dir = Path(os.environ.get("BACKUP_DIR", "/backups"))
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL is required for backups")
        try:
            key = base64.b64decode(encoded_key + "=" * (-len(encoded_key) % 4), altchars=b"-_", validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("Invalid backup encryption key") from None
        if len(key) != 32:
            raise ValueError("Backup encryption key must decode to 32 bytes")
        try:
            retention = int(os.environ.get("BACKUP_RETENTION_COUNT", "14"))
        except ValueError:
            raise ValueError("BACKUP_RETENTION_COUNT must be between 1 and 365") from None
        if not 1 <= retention <= 365:
            raise ValueError("BACKUP_RETENTION_COUNT must be between 1 and 365")
        return cls(database_url, backup_dir, key, retention)


def _connection(settings: Settings) -> dict[str, str]:
    values = conninfo_to_dict(settings.database_url)
    if not values.get("dbname"):
        raise ValueError("DATABASE_URL must name a database")
    return values


def _pg_env(values: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    if values.get("password"):
        env["PGPASSWORD"] = values["password"]
    for option, variable in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
                             ("sslcert", "PGSSLCERT"), ("sslkey", "PGSSLKEY"),
                             ("gssencmode", "PGGSSENCMODE")):
        if values.get(option):
            env[variable] = values[option]
    return env


def _tool(env_name: str, name: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    found = configured or shutil.which(name)
    if not found or not Path(found).is_file():
        raise RuntimeError(f"missing_{name}")
    return found


def _db_args(values: dict[str, str], dbname: str | None = None) -> list[str]:
    args: list[str] = []
    for option, flag in (("host", "--host"), ("port", "--port"), ("user", "--username")):
        if values.get(option):
            args.extend((flag, values[option]))
    args.extend(("--dbname", dbname or values["dbname"]))
    return args


def _encrypted_stream(source: BinaryIO, target: BinaryIO, key: bytes, digest) -> int:
    nonce = secrets.token_bytes(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    header = MAGIC + nonce
    target.write(header)
    digest.update(header)
    written = len(header)
    while chunk := source.read(CHUNK_SIZE):
        encrypted = encryptor.update(chunk)
        target.write(encrypted)
        digest.update(encrypted)
        written += len(encrypted)
    tail = encryptor.finalize()
    target.write(tail)
    digest.update(tail)
    target.write(encryptor.tag)
    digest.update(encryptor.tag)
    return written + len(tail) + TAG_BYTES


def _decrypt_to_file(path: Path, destination: Path, key: bytes) -> None:
    size = path.stat().st_size
    if size <= len(MAGIC) + NONCE_BYTES + TAG_BYTES:
        raise RuntimeError("invalid_backup_size")
    with path.open("rb") as encrypted:
        header = encrypted.read(len(MAGIC) + NONCE_BYTES)
        if len(header) != len(MAGIC) + NONCE_BYTES or not header.startswith(MAGIC):
            raise RuntimeError("invalid_backup_header")
        nonce = header[len(MAGIC):]
        encrypted.seek(-TAG_BYTES, os.SEEK_END)
        tag = encrypted.read(TAG_BYTES)
        encrypted.seek(len(header))
        remaining = size - len(header) - TAG_BYTES
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        with destination.open("wb") as plain:
            while remaining:
                chunk = encrypted.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise RuntimeError("truncated_backup")
                remaining -= len(chunk)
                plain.write(decryptor.update(chunk))
            try:
                plain.write(decryptor.finalize())
            except InvalidTag:
                raise RuntimeError("invalid_backup_authentication") from None
            plain.flush()
            os.fsync(plain.fileno())


def _checked_file(settings: Settings, name: str) -> Path:
    if not BACKUP_NAME.fullmatch(name):
        raise RuntimeError("invalid_backup_name")
    root = settings.backup_dir.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise RuntimeError("backup_file_missing")
    return path


def _prune_backups(settings: Settings, *, preserve_name: str | None = None) -> int:
    paths = sorted(
        (path for path in settings.backup_dir.iterdir()
         if path.is_file() and BACKUP_NAME.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    if preserve_name:
        paths = [path for path in paths if path.name != preserve_name]
        keep_others = max(0, settings.retention_count - 1)
    else:
        keep_others = settings.retention_count
    deleted = 0
    for path in paths[keep_others:]:
        path.unlink()
        deleted += 1
    return deleted


def backup_once(conn, settings: Settings, *, now: datetime | None = None) -> dict:
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("backup time must include a timezone")
    run_id = db.start_backup_run(conn)
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"radar-{stamp}-{secrets.token_hex(8)}.backup"
    target = settings.backup_dir / name
    partial = settings.backup_dir / (name + ".partial")
    digest = hashlib.sha256()
    size = 0
    partial_created = False
    target_created = False
    try:
        values = _connection(settings)
        command = [_tool("PG_DUMP_PATH", "pg_dump"), "--no-password", "--format=custom",
                   "--no-owner", "--no-privileges", *_db_args(values)]
        with partial.open("xb") as encrypted, tempfile.TemporaryFile() as stderr:
            partial_created = True
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, env=_pg_env(values))
            assert process.stdout is not None
            try:
                size = _encrypted_stream(process.stdout, encrypted, settings.encryption_key, digest)
            finally:
                process.stdout.close()
            return_code = process.wait()
            encrypted.flush()
            os.fsync(encrypted.fileno())
            if return_code != 0:
                raise RuntimeError("pg_dump_failed")
        if size <= len(MAGIC) + NONCE_BYTES + TAG_BYTES:
            raise RuntimeError("empty_backup")
        os.link(partial, target)
        target_created = True
        partial.unlink()
        partial_created = False
        db.finish_backup_run(conn, run_id, status="success", file_name=name,
                             size_bytes=size, sha256=digest.hexdigest())
        deleted = _prune_backups(settings, preserve_name=name)
        return {"id": run_id, "file_name": name, "size_bytes": size,
                "sha256": digest.hexdigest(), "pruned": deleted}
    except Exception as error:
        if partial_created:
            partial.unlink(missing_ok=True)
        if target_created:
            target.unlink(missing_ok=True)
        code = str(error) if re.fullmatch(r"[a-z0-9_]{1,80}", str(error)) else "backup_failed"
        db.finish_backup_run(conn, run_id, status="failed", error_code=code)
        raise RuntimeError(code) from None


def verify_latest(conn, settings: Settings, *, now: datetime | None = None) -> dict:
    row = conn.execute(
        "SELECT id, file_name, size_bytes, sha256 FROM backup_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("no_successful_backup")
    run_id, name, expected_size, expected_hash = row
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("verification time must include a timezone")
    created_name = None
    created = False
    try:
        backup_path = _checked_file(settings, name)
        if backup_path.stat().st_size != expected_size:
            raise RuntimeError("backup_size_mismatch")
        digest = hashlib.sha256()
        with backup_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), expected_hash or ""):
            raise RuntimeError("backup_checksum_mismatch")

        values = _connection(settings)
        created_name = "radar_backup_verify_" + secrets.token_hex(8)
        admin_values = {**values, "dbname": "postgres"}
        with psycopg.connect(**admin_values, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(created_name)))
            created = True

        with tempfile.TemporaryDirectory(prefix="radar-backup-verify-") as temp_dir:
            dump_path = Path(temp_dir) / "restore.dump"
            _decrypt_to_file(backup_path, dump_path, settings.encryption_key)
            restore_command = [_tool("PG_RESTORE_PATH", "pg_restore"), "--no-password", "--exit-on-error", "--no-owner",
                               "--no-privileges", *_db_args(values, created_name), str(dump_path)]
            restored = subprocess.run(restore_command, capture_output=True, env=_pg_env(values), timeout=1800)
            if restored.returncode != 0:
                raise RuntimeError("pg_restore_failed")

        restored_values = {**values, "dbname": created_name}
        with psycopg.connect(**restored_values) as restored_db:
            tables = {row[0] for row in restored_db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()}
            if not REQUIRED_TABLES.issubset(tables):
                raise RuntimeError("restore_missing_tables")
            foreign_keys = restored_db.execute(
                "SELECT count(*) FROM information_schema.table_constraints WHERE constraint_schema='public' AND constraint_type='FOREIGN KEY'"
            ).fetchone()[0]
            if foreign_keys < 1:
                raise RuntimeError("restore_missing_relationships")
        db.mark_backup_restore(conn, run_id, status="success")
        return {"id": run_id, "status": "success", "checked_at": current.isoformat()}
    except Exception as error:
        code = str(error) if re.fullmatch(r"[a-z0-9_]{1,80}", str(error)) else "restore_failed"
        db.mark_backup_restore(conn, run_id, status="failed", error_code=code)
        raise RuntimeError(code) from None
    finally:
        if created and created_name:
            try:
                admin_values = {**_connection(settings), "dbname": "postgres"}
                with psycopg.connect(**admin_values, autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(created_name)))
            except Exception:
                db.mark_backup_restore(conn, run_id, status="failed", error_code="verify_database_cleanup_failed")
