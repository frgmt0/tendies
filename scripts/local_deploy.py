#!/usr/bin/env python3
"""Run deployer's release engine locally on the Linux target, without SSH."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def _load_deployer() -> tuple[type, object]:
    root = Path(
        os.environ.get(
            "TENDIES_DEPLOYER_ROOT",
            "/home/jason/.local/share/tendies/tools/deployer",
        )
    )
    if root.is_dir():
        sys.path.insert(0, str(root))
    try:
        from deployer.config import load_config
        from deployer.remote import Runner
    except ImportError as error:
        raise SystemExit(
            "deployer package is unavailable; set TENDIES_DEPLOYER_ROOT to its checkout"
        ) from error
    return Runner, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--env", type=Path, required=True)
    args = parser.parse_args(argv)
    Runner, load_config = _load_deployer()

    class LocalRunner(Runner):
        def ssh(self, script: str, *, capture: bool = False):
            return self._run(["bash", "-s"], input=script, capture=capture)

        def _rsync(self, release: str, extra_excludes=()):
            command = ["rsync", "-az"]
            for item in dict.fromkeys((*self.config.deploy.exclude, *extra_excludes)):
                command.extend(("--exclude", item))
            self._run(command + [str(self.config.repo) + "/", release + "/"])

    repo = args.repo.expanduser().resolve()
    runner = LocalRunner(load_config(repo))
    runner.deploy(args.env.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
