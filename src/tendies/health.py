"""Private heartbeat for release readiness and a single-process SQLite lease."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

log = logging.getLogger(__name__)


def acquire_database_lease(database_url: str):
    """Prevent a second local process from advancing the same SQLite economy."""
    url = make_url(database_url)
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        return None
    target = Path(url.database).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(target) + '.lock', 'a')
    os.chmod(handle.name, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Another Tendies process already owns this database.') from None
    return handle


def write_heartbeat(path: Path, *, ready: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    release_file = Path.cwd() / '.release-id'
    release = os.getenv('TENDIES_RELEASE') or (release_file.read_text().strip() if release_file.exists() else 'development')
    payload = dict(pid=os.getpid(), ready=ready, release=release, updated_at=time.time())
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream)
    os.replace(temporary, path)


async def heartbeat(bot) -> None:
    target = os.getenv('TENDIES_HEALTH_FILE')
    if not target:
        return
    while not bot.is_closed():
        ready = bot.is_ready()
        try:
            async with bot.db.session() as session:
                await session.execute(text('SELECT 1'))
            write_heartbeat(Path(target), ready=ready)
        except Exception:
            log.exception('Readiness heartbeat failed')
        await asyncio.sleep(15)
