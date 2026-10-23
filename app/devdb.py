"""The dev Postgres in var/pg by hand: python -m app.devdb status | stop"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .db import DEV_PG


def _pg_ctl() -> str:
    import pgserver
    return str(Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_ctl")


def main(cmd: str) -> None:
    if not (DEV_PG / "PG_VERSION").exists():
        print("dev database not created yet")
        return
    if cmd == "stop":
        r = subprocess.run([_pg_ctl(), "-D", str(DEV_PG), "status"], capture_output=True, text=True)
        if r.returncode != 0:
            print("already stopped")
            return
        subprocess.run([_pg_ctl(), "-D", str(DEV_PG), "stop", "-m", "fast"], check=True,
                       capture_output=True)  # stdout stays clean: scripts read codes from it
    else:
        subprocess.run([_pg_ctl(), "-D", str(DEV_PG), "status"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "status")
