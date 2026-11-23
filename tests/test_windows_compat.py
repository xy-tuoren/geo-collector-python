import errno
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest

from geo_collector import cli, storage


@pytest.mark.parametrize("error_number", [None, errno.EACCES, errno.EIO])
def test_windows_lease_locks_first_byte_and_closes_handle(tmp_path, monkeypatch, error_number):
    lease = storage.DeviceLease(tmp_path, "device")
    lease.file.parent.mkdir()
    lease.file.write_bytes(b"existing lease")
    nonblocking_lock, unlock = 2, 0
    calls = []

    def locking(fd, mode, size):
        calls.append((fd, mode))
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert size == 1
        if error_number:
            raise OSError(error_number, "locking failed")

    monkeypatch.setattr(storage, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=nonblocking_lock, LK_UNLCK=unlock))

    if error_number is None:
        with lease:
            fd = lease.stream.fileno()
        assert calls == [(fd, nonblocking_lock), (fd, unlock)]
    elif error_number == errno.EACCES:
        with pytest.raises(RuntimeError, match="已有采集进程"):
            lease.__enter__()
    else:
        with pytest.raises(OSError, match="locking failed"):
            lease.__enter__()

    assert lease.stream is None
    with pytest.raises(OSError):
        os.fstat(calls[0][0])
    assert lease.file.read_bytes() == b"existing lease"


def test_windows_stdout_remains_utf8_when_observer_is_unavailable(tmp_path, monkeypatch):
    output = tmp_path / "captures"
    with io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as stdout, \
         io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as stderr:
        monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32", stdout=stdout, stderr=stderr))
        with redirect_stdout(stdout), redirect_stderr(stderr):
            assert cli.main(["run", "--question", "测试题", "--agent", "codex", "--output", str(output)]) == 1
        result = json.loads(stdout.buffer.getvalue().decode("utf-8"))
        assert "当前对话 Agent" in result["error"]
        assert stderr.encoding == "utf-8"
    assert not output.exists()
