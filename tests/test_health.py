import json
from pathlib import Path

import pytest

from tendies.health import acquire_database_lease, write_heartbeat


def test_sqlite_lease_blocks_a_second_owner_and_releases(tmp_path):
    url = 'sqlite+aiosqlite:///' + str(tmp_path / 'game.db')
    first = acquire_database_lease(url)
    try:
        with pytest.raises(RuntimeError, match='already owns'):
            acquire_database_lease(url)
    finally:
        first.close()
    second = acquire_database_lease(url)
    second.close()


def test_heartbeat_is_private_and_identifies_actual_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('TENDIES_RELEASE', raising=False)
    Path('.release-id').write_text('release-test')
    target = tmp_path / 'state' / 'health.json'
    write_heartbeat(target, ready=True)
    payload = json.loads(target.read_text())
    assert payload['ready'] is True
    assert payload['release'] == 'release-test'
    assert target.stat().st_mode & 0o777 == 0o600
    write_heartbeat(target, ready=False)
    assert json.loads(target.read_text())['ready'] is False
