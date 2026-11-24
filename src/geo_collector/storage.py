from __future__ import annotations

import errno
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_echo_events = ContextVar("geo_collect_echo_events", default=False)


@contextmanager
def event_output(enabled=False):
    """Keep detailed local evidence without streaming it into an Agent context."""
    token = _echo_events.set(bool(enabled))
    try:
        yield
    finally:
        _echo_events.reset(token)


def read_json(file: Path):
    return json.loads(Path(file).read_text(encoding="utf-8"))


def write_json(file: Path, value, *, fsync: bool = True):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)


def event(file: Path, name: str, **details):
    value = {"time": time.time(), "event": name, **details}
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    if _echo_events.get():
        print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)


class DeviceLease:
    """One local collector per phone; a paused run releases the device."""

    def __init__(self, root: Path, serial: str):
        import hashlib
        self.file = root / "locks" / (hashlib.sha256(serial.encode()).hexdigest()[:20] + ".lock")
        self.stream = None

    def __enter__(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        stream = self.file.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            stream.close()
            if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError("已有采集进程控制这台手机，请等待该进程退出。") from None
            raise
        self.stream = stream
        return self

    def __exit__(self, *_):
        stream, self.stream = self.stream, None
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            stream.close()
