"""Tests for scrcpy live screen mirroring, automatic installation, and CLI integration."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

import pytest

from geo_collector import cli, device, screen
from geo_collector.recovery import NeedsRepair
from geo_collector.storage import DeviceLease


def _make_tar_gz(entries: list[tuple[str, bytes | str, str]]) -> bytes:
    """Create a .tar.gz archive in memory. Each entry is (name, content_or_link, kind)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name=name)
            if kind == "file":
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                info.size = len(data)
                info.mode = 0o755 if name.endswith("scrcpy") else 0o644
                tf.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(payload)
                tf.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
    return buf.getvalue()


def _make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeHttpResponse:
    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_platform_asset_mapping_and_unsupported_arch():
    assert screen.platform_asset("darwin", "arm64")[0].startswith("scrcpy-macos-aarch64-")
    assert screen.platform_asset("darwin", "x86_64")[0].startswith("scrcpy-macos-x86_64-")
    assert screen.platform_asset("win32", "AMD64")[0].startswith("scrcpy-win64-")
    assert screen.platform_asset("win32", "arm64")[0].startswith("scrcpy-winarm64-")
    assert screen.platform_asset("win32", "x86")[0].startswith("scrcpy-win32-")
    assert screen.platform_asset("linux", "x86_64")[0].startswith("scrcpy-linux-x86_64-")
    with pytest.raises(screen.ScreenError, match="暂无官方预编译"):
        screen.platform_asset("linux", "aarch64")


def test_build_download_url_supports_mirror_and_explicit_url(monkeypatch):
    monkeypatch.delenv("GEO_COLLECT_SCRCPY_URL", raising=False)
    monkeypatch.delenv("GEO_COLLECT_SCRCPY_MIRROR", raising=False)
    asset = f"scrcpy-macos-aarch64-{screen.SCRCPY_VERSION}.tar.gz"
    official = f"{screen.SCRCPY_RELEASE_BASE}/{asset}"
    assert screen.build_download_url(asset) == official
    assert screen.build_download_url(asset, mirror_prefix="https://ghfast.top/") == f"https://ghfast.top/{official}"
    assert screen.build_download_url(asset, mirror_prefix="https://mirror.example/files/{asset}") == f"https://mirror.example/files/{asset}"
    assert screen.build_download_url(asset, download_url="https://cdn.example/scrcpy.tar.gz") == "https://cdn.example/scrcpy.tar.gz"
    with pytest.raises(ValueError, match="只能提供一个"):
        screen.build_download_url(asset, download_url="https://a.test/x", mirror_prefix="https://b.test/")
    with pytest.raises(ValueError, match="http/https"):
        screen.build_download_url(asset, download_url="file:///etc/passwd")


def test_install_scrcpy_tar_verifies_sha256_extracts_internal_symlinks_and_is_idempotent(tmp_path, monkeypatch):
    archive = _make_tar_gz([
        ("scrcpy-macos-aarch64-v5.0.1/scrcpy", b"#!/bin/sh\necho ok\n", "file"),
        ("scrcpy-macos-aarch64-v5.0.1/scrcpy-server", b"server-bytes", "file"),
        ("scrcpy-macos-aarch64-v5.0.1/libfoo.1.0.dylib", b"dylib-bytes", "file"),
        ("scrcpy-macos-aarch64-v5.0.1/libfoo.1.dylib", "libfoo.1.0.dylib", "symlink"),
    ])
    digest = hashlib.sha256(archive).hexdigest()
    asset_name = f"scrcpy-macos-aarch64-{screen.SCRCPY_VERSION}.tar.gz"
    monkeypatch.setitem(screen.SCRCPY_ASSETS, ("darwin", "arm64"), (asset_name, digest))
    downloads = []

    def fake_urlopen(req, timeout=120):
        downloads.append(req.full_url)
        return _FakeHttpResponse(archive)

    monkeypatch.setattr(screen, "urlopen", fake_urlopen)
    cache_root = tmp_path / "scrcpy-cache"

    first = screen.install_scrcpy(cache_root=cache_root, system="darwin", machine="arm64")
    assert first.source == "installed"
    assert first.version == screen.SCRCPY_VERSION
    assert Path(first.executable).is_file()
    assert first.server_path and Path(first.server_path).is_file()
    assert (Path(first.executable).parent / "libfoo.1.dylib").is_symlink()
    assert (Path(first.executable).parent / screen.SCRCPY_RECEIPT).is_file()
    assert len(downloads) == 1

    # Second call reuses cached install without downloading again.
    second = screen.install_scrcpy(cache_root=cache_root, system="darwin", machine="arm64")
    assert second.source == "managed"
    assert second.executable == first.executable
    assert len(downloads) == 1

    # force=True redownloads cleanly.
    third = screen.install_scrcpy(cache_root=cache_root, force=True, system="darwin", machine="arm64")
    assert third.source == "installed"
    assert len(downloads) == 2


def test_install_scrcpy_zip_on_windows_and_rejects_bad_sha256_without_leftovers(tmp_path, monkeypatch):
    archive = _make_zip([
        ("scrcpy-win64-v5.0.1/scrcpy.exe", b"MZ-fake-exe"),
        ("scrcpy-win64-v5.0.1/scrcpy-server", b"server-bytes"),
    ])
    digest = hashlib.sha256(archive).hexdigest()
    asset_name = f"scrcpy-win64-{screen.SCRCPY_VERSION}.zip"
    monkeypatch.setitem(screen.SCRCPY_ASSETS, ("win32", "amd64"), (asset_name, digest))
    monkeypatch.setattr(screen, "urlopen", lambda req, timeout=120: _FakeHttpResponse(archive))

    cache_root = tmp_path / "win-cache"
    installed = screen.install_scrcpy(cache_root=cache_root, system="win32", machine="AMD64")
    assert installed.source == "installed"
    assert installed.executable.endswith("scrcpy.exe")
    assert installed.server_path and installed.server_path.endswith("scrcpy-server")

    # Corrupted payload must fail SHA256 verification and clean staging directory.
    bad_cache = tmp_path / "bad-cache"
    monkeypatch.setattr(screen, "urlopen", lambda req, timeout=120: _FakeHttpResponse(b"corrupted"))
    with pytest.raises(screen.ScreenError, match="SHA256 校验失败"):
        screen.install_scrcpy(cache_root=bad_cache, system="win32", machine="AMD64")
    assert list(bad_cache.iterdir()) == []


@pytest.mark.parametrize("bad_entries", [
    [("../escape.txt", b"bad", "file")],
    [("/etc/evil", b"bad", "file")],
    [("dir/link", "../../outside", "symlink")],
    [("dir/abslink", "/etc/passwd", "symlink")],
])
def test_safe_extract_tar_rejects_traversal_and_escaping_symlinks(tmp_path, bad_entries):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(_make_tar_gz(bad_entries))
    with pytest.raises(screen.ScreenError):
        screen._safe_extract_tar(archive, tmp_path / "dest")


def test_safe_extract_zip_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_make_zip([("../evil.exe", b"nope")]))
    with pytest.raises(screen.ScreenError):
        screen._safe_extract_zip(archive, tmp_path / "dest")


def test_resolve_scrcpy_precedence_and_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("GEO_COLLECT_SCRCPY_PATH", raising=False)
    monkeypatch.delenv("GEO_COLLECT_SCRCPY_URL", raising=False)
    monkeypatch.delenv("GEO_COLLECT_SCRCPY_MIRROR", raising=False)

    # 1. Explicit path takes precedence.
    custom = tmp_path / "custom-scrcpy"
    custom.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(screen.shutil, "which", lambda name: "/usr/local/bin/scrcpy")
    got = screen.resolve_scrcpy(scrcpy_path=str(custom))
    assert got.source == "configured" and got.executable == str(custom)

    with pytest.raises(screen.ScreenError, match="不存在"):
        screen.resolve_scrcpy(scrcpy_path=str(tmp_path / "missing-scrcpy"))

    # 2. System PATH comes before managed cache.
    got_sys = screen.resolve_scrcpy()
    assert got_sys.source == "system" and got_sys.executable == "/usr/local/bin/scrcpy"

    # 3. No system binary -> dry_run previews auto-install without writing anything.
    monkeypatch.setattr(screen.shutil, "which", lambda name: None)
    preview = screen.resolve_scrcpy(dry_run=True, system="darwin", machine="arm64")
    assert preview.source == "would_install"
    assert not (tmp_path / ".cache").exists()

    # 4. auto_install=False fails clearly when absent.
    with pytest.raises(screen.ScreenError, match="已禁用自动安装"):
        screen.resolve_scrcpy(auto_install=False)


def test_build_command_and_env_defaults_readonly_and_pins_adb():
    inst = screen.ScrcpyInstallation(
        executable="/cache/scrcpy",
        source="managed",
        version=screen.SCRCPY_VERSION,
        server_path="/cache/scrcpy-server",
    )
    cmd = screen.build_scrcpy_command(inst, serial="ABC123")
    assert cmd == [
        "/cache/scrcpy",
        "-s", "ABC123",
        "--no-control",
        "--no-audio",
        "--max-size=1080",
        "--max-fps=30",
        "--window-title=geo-collect | ABC123",
    ]
    cmd_ctrl = screen.build_scrcpy_command(
        inst, serial="ABC123", control=True, audio=True, max_size=0, max_fps=0, always_on_top=True, title="Phone"
    )
    assert cmd_ctrl == ["/cache/scrcpy", "-s", "ABC123", "--always-on-top", "--window-title=Phone"]

    env = screen.build_scrcpy_env(inst, adb_path="/opt/adb", base_env={"PATH": "/bin"})
    assert env["ADB"] == "/opt/adb"
    assert env["SCRCPY_SERVER_PATH"] == "/cache/scrcpy-server"

    sys_inst = screen.ScrcpyInstallation(executable="/opt/homebrew/bin/scrcpy", source="system")
    sys_env = screen.build_scrcpy_env(sys_inst, adb_path="/opt/adb", base_env={"PATH": "/bin"})
    assert sys_env["ADB"] == "/opt/adb"
    assert "SCRCPY_SERVER_PATH" not in sys_env

    cfg_inst = screen.ScrcpyInstallation(
        executable="/custom/scrcpy", source="configured", server_path="/custom/scrcpy-server"
    )
    cfg_env = screen.build_scrcpy_env(cfg_inst, adb_path="/opt/adb", base_env={"PATH": "/bin"})
    assert cfg_env["SCRCPY_SERVER_PATH"] == "/custom/scrcpy-server"


def test_screen_cli_install_only_and_concurrent_with_device_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(device, "Device", lambda **_: pytest.fail("screen must not instantiate uiautomator2 Device"))
    monkeypatch.setattr(screen.shutil, "which", lambda name: "/opt/bin/scrcpy" if name == "scrcpy" else None)

    # --install-only does not need a phone or ADB.
    res = cli.execute(cli.parser().parse_args(["screen", "--install-only"]))
    assert res["status"] == "ready" and res["scrcpy"] == "/opt/bin/scrcpy" and res["source"] == "system"

    # Even while a run holds DeviceLease on the same phone, `geo-collect screen` still works.
    monkeypatch.setattr(screen, "select_adb_device", lambda serial=None, adb=None: ("/opt/bin/adb", "DEV-1"))
    with DeviceLease(tmp_path / ".cache" / "geo-collector", "DEV-1"):
        dry = cli.execute(cli.parser().parse_args(["screen", "--serial", "DEV-1", "--dry-run"]))
        assert dry["status"] == "preview"
        assert dry["serial"] == "DEV-1"
        assert dry["readonly"] is True
        assert "--no-control" in dry["command"]


def test_screen_cli_background_and_foreground_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(screen.shutil, "which", lambda name: "/opt/bin/scrcpy" if name == "scrcpy" else None)
    monkeypatch.setattr(screen, "select_adb_device", lambda serial=None, adb=None: ("/opt/bin/adb", "DEV-2"))

    spawned = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            self.pid = 4242
            self.returncode = None
            self._cmd = cmd
            self._kwargs = kwargs
            spawned.append((cmd, kwargs))

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

    monkeypatch.setattr(screen.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(screen.time, "sleep", lambda _: None)

    bg = cli.execute(cli.parser().parse_args(["screen", "--background", "--control"]))
    assert bg["status"] == "started" and bg["pid"] == 4242 and bg["readonly"] is False
    assert "--no-control" not in spawned[-1][0]
    assert spawned[-1][1]["env"]["ADB"] == "/opt/bin/adb"

    fg = cli.execute(cli.parser().parse_args(["screen"]))
    assert fg["status"] == "closed" and fg["returncode"] == 0 and fg["readonly"] is True
    assert "--no-control" in spawned[-1][0]


def test_run_with_screen_flag_starts_and_cleans_up_on_success_and_needs_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    mirror_events = []

    @contextmanager
    def fake_screen_mirror(**kwargs):
        mirror_events.append(("enter", kwargs))
        try:
            yield {"event": "screen_started"}
        finally:
            mirror_events.append(("exit", kwargs["serial"]))

    class FakeDevice:
        def __init__(self, serial=None, adb=None):
            self.serial = serial or "SER-99"
            self.adb = adb or "/opt/adb"

        def close(self):
            pass

    monkeypatch.setattr(device, "Device", FakeDevice)
    monkeypatch.setattr(screen, "screen_mirror", fake_screen_mirror)
    import geo_collector.workflow as wf

    # 1. Normal completion closes mirror cleanly.
    monkeypatch.setattr(wf, "run_questions", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "verify_run", lambda directory: {"complete": True, "run_directory": str(directory)})
    out = cli.execute(cli.parser().parse_args([
        "run", "--question", "测试问题", "--entry", "xiaohe",
        "--output", str(tmp_path / "cap1"), "--rule-dir", str(tmp_path / "rules"), "--screen",
    ]))
    assert out["complete"] is True
    assert mirror_events[0][0] == "enter"
    assert mirror_events[0][1]["serial"] == "SER-99"
    assert mirror_events[0][1]["adb_path"] == "/opt/adb"
    assert mirror_events[0][1]["control"] is False
    assert mirror_events[1] == ("exit", "SER-99")

    # 2. NeedsRepair (exit 20) still closes mirror cleanly and --screen-control enables control.
    mirror_events.clear()
    req_file = tmp_path / "req.json"
    req_file.write_text("{}", encoding="utf-8")

    def raise_repair(*a, **kw):
        raise NeedsRepair(str(req_file))

    monkeypatch.setattr(wf, "run_questions", raise_repair)
    with pytest.raises(NeedsRepair):
        cli.execute(cli.parser().parse_args([
            "run", "--question", "测试问题", "--entry", "xiaohe",
            "--output", str(tmp_path / "cap2"), "--rule-dir", str(tmp_path / "rules"), "--screen-control",
        ]))
    assert mirror_events[0][0] == "enter" and mirror_events[0][1]["control"] is True
    assert mirror_events[1] == ("exit", "SER-99")
