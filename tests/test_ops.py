from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from tendies import ops


def make_database(path: Path, value: str = "original") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))


def read_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM sample").fetchone()[0]


def test_sqlite_path_understands_relative_and_absolute_sqlalchemy_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert ops.sqlite_path("sqlite+aiosqlite:///relative.db") == tmp_path / "relative.db"
    assert ops.sqlite_path("sqlite+aiosqlite:////var/lib/tendies.db") == Path("/var/lib/tendies.db").resolve()
    with pytest.raises(ops.OperationsError, match="requires a SQLite"):
        ops.sqlite_path("postgresql+asyncpg://localhost/tendies")


def test_online_backup_is_consistent_verified_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "live.db"
    make_database(database)
    monkeypatch.setenv("TENDIES_DATA_DIR", str(tmp_path / "state"))

    backup = ops.backup_database(source=database, reason="predeploy")

    assert backup is not None
    assert read_value(backup) == "original"
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sample SET value = 'changed'")
    assert read_value(backup) == "original"


def test_first_deploy_backup_is_a_successful_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENDIES_DATA_DIR", str(tmp_path / "state"))
    assert ops.backup_database(source=tmp_path / "missing.db") is None


def test_restore_verifies_source_and_preserves_pre_restore_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TENDIES_DATA_DIR", str(state))
    live = state / "tendies.db"
    snapshot = tmp_path / "known-good.sqlite3"
    state.mkdir()
    make_database(live, "live")
    make_database(snapshot, "restored")
    Path(str(live) + "-wal").write_bytes(b"stale wal")
    Path(str(live) + "-shm").write_bytes(b"stale shm")

    assert ops.restore_database(snapshot, destination=live) == live

    assert read_value(live) == "restored"
    assert not Path(str(live) + "-wal").exists()
    assert not Path(str(live) + "-shm").exists()
    preserved = list((state / "backups").glob("*-pre-restore.sqlite3"))
    assert len(preserved) == 1
    assert read_value(preserved[0]) == "live"

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(ops.OperationsError, match="database check failed"):
        ops.restore_database(corrupt, destination=live)
    assert read_value(live) == "restored"


def test_restore_stages_selected_backup_before_retention_can_prune_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    backup_dir = state / "backups"
    backup_dir.mkdir(parents=True)
    monkeypatch.setenv("TENDIES_DATA_DIR", str(state))
    live = state / "tendies.db"
    selected = backup_dir / "tendies-20000101T000000Z-manual.sqlite3"
    make_database(live, "live")
    make_database(selected, "selected")
    os.utime(selected, (1, 1))
    for index in range(30):
        newer = backup_dir / f"tendies-20260917T{index:06d}Z-daily.sqlite3"
        shutil_source = tmp_path / f"snapshot-{index}.sqlite3"
        make_database(shutil_source, f"newer-{index}")
        newer.write_bytes(shutil_source.read_bytes())
        os.utime(newer, (100 + index, 100 + index))

    ops.restore_database(selected, destination=live)

    assert read_value(live) == "selected"
    assert not selected.exists()


def test_restore_refuses_the_database_lease_held_by_the_running_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("TENDIES_DATA_DIR", str(state))
    live = state / "tendies.db"
    snapshot = tmp_path / "known-good.sqlite3"
    make_database(live, "live")
    make_database(snapshot, "restored")
    lock = Path(str(live) + ".lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ops.OperationsError, match="database is active"):
            ops.restore_database(snapshot, destination=live)
    finally:
        lock.close()

    assert read_value(live) == "live"


def test_healthcheck_binds_fresh_live_process_to_release_and_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tendies.db"
    make_database(database)
    health = tmp_path / "health.json"
    now = time.time()
    payload = {
        "pid": os.getpid(),
        "ready": True,
        "release": "release-20260917T010203Z-test",
        "updated_at": now - 5,
    }
    health.write_text(json.dumps(payload), encoding="utf-8")

    assert ops.check_health(
        health_file=health,
        database=database,
        release=payload["release"],
        now=now,
        max_age=90,
    ) == payload

    payload["release"] = "release-old"
    health.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ops.OperationsError, match="does not match"):
        ops.check_health(
            health_file=health,
            database=database,
            release="release-new",
            now=now,
        )


def test_healthcheck_rejects_stale_or_dead_process(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tendies.db"
    make_database(database)
    health = tmp_path / "health.json"
    payload = {
        "pid": os.getpid(),
        "ready": True,
        "release": "release-test",
        "updated_at": 100.0,
    }
    health.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ops.OperationsError, match="stale"):
        ops.check_health(
            health_file=health,
            database=database,
            release="release-test",
            now=200.0,
            max_age=90,
        )

    payload.update(pid=999_999_999, updated_at=200.0)
    health.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ops.OperationsError, match="not running"):
        ops.check_health(
            health_file=health,
            database=database,
            release="release-test",
            now=200.0,
        )

    payload.update(pid=os.getpid(), updated_at=float("nan"))
    health.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ops.OperationsError, match="timestamp is invalid"):
        ops.check_health(
            health_file=health,
            database=database,
            release="release-test",
            now=200.0,
        )


def test_release_stamp_uses_immutable_release_directory(tmp_path: Path) -> None:
    release = tmp_path / "release-20260917T010203Z-test"
    release.mkdir()

    assert ops.stamp_release(release) == release.name
    assert (release / ".release-id").read_text(encoding="utf-8") == release.name + "\n"

    with pytest.raises(ops.OperationsError, match="release directory"):
        ops.stamp_release(tmp_path)


def test_deployment_contract_is_rollback_safe_and_persistent() -> None:
    root = Path(__file__).parents[1]
    manifest = (root / "deploy.toml").read_text(encoding="utf-8")
    pipeline = (root / "scripts" / "pipeline.sh").read_text(encoding="utf-8")

    assert 'restart = "always"' in manifest
    assert 'file = ".env.prod"' in manifest
    assert "sqlite+aiosqlite:////home/jason/.local/share/tendies/tendies.db" in manifest
    assert "backup --reason predeploy" in manifest
    assert "/usr/bin/python3 src/tendies/ops.py healthcheck" in manifest
    assert "/home/jason/.local/bin/uv sync --frozen --no-dev" in manifest
    assert "LocalRunner(Runner)" in (root / "scripts" / "local_deploy.py").read_text(encoding="utf-8")
    assert "git reset" not in pipeline
    assert 'flock -n 9' in pipeline
    assert "production deploy requires a clean committed checkout" in pipeline


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def _poll_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str], str]:
    project = Path(__file__).parents[1]
    source = tmp_path / "source"
    origin = tmp_path / "origin.git"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "pipeline.sh").write_bytes(
        (project / "scripts" / "pipeline.sh").read_bytes()
    )
    (scripts / "pipeline.sh").chmod(0o755)
    (scripts / "local_deploy.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['LOCAL_DEPLOY_MARKER']).write_text('called')\n",
        encoding="utf-8",
    )
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Pipeline Test", cwd=source)
    _git("config", "user.email", "pipeline@example.invalid", cwd=source)
    _git("add", ".", cwd=source)
    _git("commit", "-m", "candidate", cwd=source)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    _git("remote", "add", "origin", str(origin), cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    revision = _git("rev-parse", "HEAD", cwd=source)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "flock").chmod(0o755)
    uv_log = tmp_path / "uv-called"
    (fake_bin / "uv").write_text(
        "#!/bin/sh\nprintf called > \"$UV_LOG\"\nexit 47\n", encoding="utf-8"
    )
    (fake_bin / "uv").chmod(0o755)
    secrets = tmp_path / "environment"
    secrets.write_text('DISCORD_TOKEN="test"\n', encoding="utf-8")
    deploy_base = tmp_path / "deploy"
    marker = tmp_path / "local-deploy-called"
    environment = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "TENDIES_DATA_DIR": str(tmp_path / "state"),
        "TENDIES_SOURCE_REPO": str(source),
        "TENDIES_DEPLOY_BASE": str(deploy_base),
        "TENDIES_SECRETS_FILE": str(secrets),
        "LOCAL_DEPLOY_MARKER": str(marker),
        "UV_LOG": str(uv_log),
    }
    return source, deploy_base, marker, environment, revision


def test_poll_main_failed_candidate_check_never_deploys(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    _, _, marker, environment, _ = _poll_fixture(tmp_path)

    result = subprocess.run(
        [str(project / "scripts" / "pipeline.sh"), "poll-main"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 47
    assert not marker.exists()
    assert Path(environment["UV_LOG"]).read_text(encoding="utf-8") == "called"


def test_poll_main_unchanged_revision_is_a_noop(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    _, deploy_base, marker, environment, revision = _poll_fixture(tmp_path)
    current = deploy_base / "current"
    current.mkdir(parents=True)
    (current / ".source-revision").write_text(revision + "\n", encoding="utf-8")

    result = subprocess.run(
        [str(project / "scripts" / "pipeline.sh"), "poll-main"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert not Path(environment["UV_LOG"]).exists()
