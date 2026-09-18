"""Small, dependency-free production operations for Tendies.

This module deliberately uses only the Python standard library so backups and
health checks still work before a release virtual environment has been built.
It can be executed directly (``python src/tendies/ops.py``) or as a module.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit


DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "tendies"


class OperationsError(RuntimeError):
    """A production invariant was not satisfied."""


@contextmanager
def _exclusive_database_lease(database: Path):
    """Acquire the same advisory lease held by the running bot."""
    lock_path = Path(str(database) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = lock_path.open("a")
    os.chmod(lock_path, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationsError(
                "database is active; stop the Tendies service before restoring"
            ) from error
        yield
    finally:
        handle.close()


def data_dir() -> Path:
    return Path(os.environ.get("TENDIES_DATA_DIR", DEFAULT_DATA_DIR)).expanduser()


def sqlite_path(database_url: str | None = None) -> Path:
    """Resolve the configured file-backed SQLite database path."""
    value = database_url or os.environ.get(
        "DATABASE_URL", f"sqlite+aiosqlite:///{data_dir() / 'tendies.db'}"
    )
    parsed = urlsplit(value)
    if parsed.scheme not in {"sqlite", "sqlite+aiosqlite"}:
        raise OperationsError("production backup requires a SQLite DATABASE_URL")
    if parsed.netloc or not parsed.path:
        raise OperationsError("DATABASE_URL must name a local SQLite file")
    # SQLAlchemy's three-slash form is relative even though urlsplit exposes a
    # leading slash; four slashes represent an absolute POSIX path.
    encoded = value.split("///", 1)[1]
    path = Path(unquote(encoded))
    if not encoded.startswith("/"):
        path = Path.cwd() / path
    return path.resolve()


def _check_database(path: Path) -> None:
    if not path.is_file():
        raise OperationsError(f"database does not exist: {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        raise OperationsError(f"database check failed: {error}") from error
    if result != ("ok",):
        raise OperationsError(f"database quick_check failed: {result!r}")


def backup_database(
    *, reason: str = "manual", source: Path | None = None, keep: int = 30
) -> Path | None:
    """Create and verify a transactionally consistent online SQLite backup.

    SQLite's backup API takes a coherent snapshot while the live bot continues
    to read and write. A missing database is normal on the first deployment.
    """
    if keep < 1:
        raise OperationsError("backup retention must be at least one")
    if not reason or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in reason.lower()):
        raise OperationsError("backup reason may contain only letters, digits, dash, and underscore")
    source = (source or sqlite_path()).resolve()
    if not source.exists():
        return None
    backup_dir = data_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = backup_dir / f"tendies-{stamp}-{reason.lower()}.sqlite3"
    counter = 1
    while destination.exists():
        destination = backup_dir / f"tendies-{stamp}-{reason.lower()}-{counter}.sqlite3"
        counter += 1
    fd, temporary_name = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=backup_dir)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(source, timeout=30) as live, sqlite3.connect(temporary) as snapshot:
            live.backup(snapshot)
        _check_database(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    backups = sorted(backup_dir.glob("tendies-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in backups[keep:]:
        expired.unlink()
    return destination


def restore_database(backup: Path, *, destination: Path | None = None) -> Path:
    """Verify and atomically restore a stopped service's database.

    The caller is responsible for proving the service is stopped. The previous
    database is first captured as a normal ``pre-restore`` backup.
    """
    backup = backup.expanduser().resolve()
    destination = (destination or sqlite_path()).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _exclusive_database_lease(destination):
        _check_database(backup)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".restore-", suffix=".sqlite3", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            # Stage the selected snapshot before retention runs. If it is an
            # older file in our backup directory, pre-restore may prune it.
            shutil.copyfile(backup, temporary)
            _check_database(temporary)
            if destination.exists():
                backup_database(reason="pre-restore", source=destination)
            os.chmod(temporary, 0o600)
            # Remove stale frames before the new main database becomes visible.
            # The matching lease prevents the bot from opening the database
            # during this window, even if systemd is trying to restart it.
            Path(str(destination) + "-wal").unlink(missing_ok=True)
            Path(str(destination) + "-shm").unlink(missing_ok=True)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def _process_is_alive(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def expected_release(cwd: Path | None = None) -> str:
    override = os.environ.get("TENDIES_RELEASE", "").strip()
    if override:
        return override
    marker = (cwd or Path.cwd()) / ".release-id"
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise OperationsError(f"release marker is unreadable: {marker}") from error
    if not value or "/" in value or "\x00" in value:
        raise OperationsError("release marker is invalid")
    return value


def check_health(
    *,
    health_file: Path | None = None,
    database: Path | None = None,
    release: str | None = None,
    now: float | None = None,
    max_age: float | None = None,
) -> dict[str, object]:
    """Validate process identity, release identity, freshness, and SQLite."""
    health_file = health_file or Path(
        os.environ.get("TENDIES_HEALTH_FILE", data_dir() / "health.json")
    )
    try:
        payload = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationsError(f"health file is unreadable: {health_file}") from error
    if not isinstance(payload, dict):
        raise OperationsError("health payload must be a JSON object")
    if payload.get("ready") is not True:
        raise OperationsError("bot is not ready")
    wanted_release = release or expected_release()
    if payload.get("release") != wanted_release:
        raise OperationsError("health release does not match the active release")
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not _process_is_alive(pid):
        raise OperationsError("health process is not running")
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        raise OperationsError("health timestamp is invalid")
    allowed_age = max_age if max_age is not None else float(os.environ.get("TENDIES_HEALTH_MAX_AGE", "90"))
    if not math.isfinite(float(updated_at)) or not math.isfinite(allowed_age) or allowed_age <= 0:
        raise OperationsError("health timestamp is invalid")
    age = (now if now is not None else time.time()) - float(updated_at)
    if age < -5 or age > allowed_age:
        raise OperationsError("health heartbeat is stale")
    _check_database((database or sqlite_path()).resolve())
    return payload


def stamp_release(cwd: Path | None = None) -> str:
    directory = (cwd or Path.cwd()).resolve()
    release = directory.name
    if not release.startswith("release-"):
        raise OperationsError("release setup must run from a deployer release directory")
    marker = directory / ".release-id"
    marker.write_text(release + "\n", encoding="utf-8")
    os.chmod(marker, 0o644)
    return release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tendies production operations")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--reason", default="manual")
    backup.add_argument("--keep", type=int, default=int(os.environ.get("TENDIES_BACKUP_KEEP", "30")))
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    commands.add_parser("healthcheck")
    commands.add_parser("stamp-release")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = backup_database(reason=args.reason, keep=args.keep)
            print(path if path is not None else "No database exists yet; no backup needed.")
        elif args.command == "restore":
            print(restore_database(args.backup))
        elif args.command == "healthcheck":
            check_health()
            print("healthy")
        elif args.command == "stamp-release":
            print(stamp_release())
        return 0
    except OperationsError as error:
        print(f"tendies ops: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
