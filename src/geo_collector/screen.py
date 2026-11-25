"""Live Android screen mirroring powered by scrcpy with automatic managed install."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile

from .device import DeviceError, _resolve_adb

SCRCPY_VERSION = "v5.0.1"
SCRCPY_RELEASE_BASE = f"https://github.com/Genymobile/scrcpy/releases/download/{SCRCPY_VERSION}"
SCRCPY_RECEIPT = ".geo-collector-scrcpy.json"

# Official Genymobile/scrcpy v5.0.1 release archives and SHA256SUMS.txt digests.
SCRCPY_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): (
        f"scrcpy-macos-aarch64-{SCRCPY_VERSION}.tar.gz",
        "33611e51977a8289e2e124b220f727895181ef2eb9060e0987a4dc2db9cf0d6b",
    ),
    ("darwin", "x86_64"): (
        f"scrcpy-macos-x86_64-{SCRCPY_VERSION}.tar.gz",
        "31a5467a9e907f162b093267cbbab1276b15c6e709c2ea8159566a8d165beb50",
    ),
    ("win32", "amd64"): (
        f"scrcpy-win64-{SCRCPY_VERSION}.zip",
        "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5",
    ),
    ("win32", "arm64"): (
        f"scrcpy-winarm64-{SCRCPY_VERSION}.zip",
        "16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b",
    ),
    ("win32", "x86"): (
        f"scrcpy-win32-{SCRCPY_VERSION}.zip",
        "0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad",
    ),
    ("linux", "x86_64"): (
        f"scrcpy-linux-x86_64-{SCRCPY_VERSION}.tar.gz",
        "9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e",
    ),
}


class ScreenError(RuntimeError):
    """Unable to prepare or run the live screen mirror."""


@dataclass(frozen=True)
class ScrcpyInstallation:
    executable: str
    source: str
    version: str | None = None
    server_path: str | None = None
    installed_dir: str | None = None

    def to_dict(self) -> dict:
        data = {"scrcpy": self.executable, "source": self.source}
        if self.version:
            data["version"] = self.version
        if self.server_path:
            data["server_path"] = self.server_path
        if self.installed_dir:
            data["installed_dir"] = self.installed_dir
        return data


def scrcpy_cache_root(cache_root: Path | None = None) -> Path:
    return (cache_root or (Path.home() / ".cache" / "geo-collector" / "scrcpy")).expanduser().resolve()


def _normalize_platform(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    raw_sys = (system or sys.platform).strip().lower()
    if raw_sys.startswith("darwin"):
        norm_sys = "darwin"
    elif raw_sys.startswith("win"):
        norm_sys = "win32"
    elif raw_sys.startswith("linux"):
        norm_sys = "linux"
    else:
        norm_sys = raw_sys

    raw_arch = (machine or platform.machine()).strip().lower()
    if raw_arch in {"arm64", "aarch64"}:
        norm_arch = "arm64"
    elif raw_arch in {"x86_64", "amd64", "x64"}:
        norm_arch = "amd64" if norm_sys == "win32" else "x86_64"
    elif raw_arch in {"x86", "i386", "i686"}:
        norm_arch = "x86"
    else:
        norm_arch = raw_arch
    return norm_sys, norm_arch


def platform_asset(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    key = _normalize_platform(system, machine)
    asset = SCRCPY_ASSETS.get(key)
    if asset is None:
        raise ScreenError(
            f"当前平台 {key[0]}/{key[1]} 暂无官方预编译 scrcpy 自动安装包，请手动安装 scrcpy 或通过 --scrcpy 指定路径。"
        )
    return asset


def _validate_http_url(url: str, label: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} 必须是有效的 http/https 地址：{url}")
    return url


def build_download_url(
    asset_name: str,
    *,
    download_url: str | None = None,
    mirror_prefix: str | None = None,
) -> str:
    if download_url and mirror_prefix:
        raise ValueError("--download-url 和 --mirror-prefix 只能提供一个")
    explicit_url = download_url or os.environ.get("GEO_COLLECT_SCRCPY_URL")
    if explicit_url:
        return _validate_http_url(explicit_url.strip(), "scrcpy 下载地址")
    official = f"{SCRCPY_RELEASE_BASE}/{asset_name}"
    prefix = mirror_prefix or os.environ.get("GEO_COLLECT_SCRCPY_MIRROR")
    if not prefix:
        return official
    prefix = prefix.strip()
    if "{url}" in prefix or "{asset}" in prefix:
        url = prefix.replace("{url}", official).replace("{asset}", asset_name)
    else:
        url = f"{prefix.rstrip('/')}/{official}"
    return _validate_http_url(url, "scrcpy 镜像地址")


def _binary_name(system: str | None = None) -> str:
    norm_sys, _ = _normalize_platform(system, "x86_64")
    return "scrcpy.exe" if norm_sys == "win32" else "scrcpy"


def _locate_managed_binary(install_dir: Path, *, system: str | None = None) -> tuple[Path, Path | None] | None:
    if not install_dir.is_dir():
        return None
    target_name = _binary_name(system)
    candidates = [install_dir / target_name]
    candidates.extend(sorted(install_dir.rglob(target_name)))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            server = candidate.parent / "scrcpy-server"
            return candidate, server if server.is_file() else None
    return None


def _ensure_within(root: Path, target: Path, member_name: str) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and not resolved_target.is_relative_to(resolved_root):
        raise ScreenError(f"scrcpy 压缩包包含越界路径，已拒绝解压：{member_name}")
    return resolved_target


def _validate_relative_member(name: str) -> Path:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ScreenError(f"scrcpy 压缩包包含非法绝对路径：{name}")
    rel = Path(normalized)
    if ".." in rel.parts:
        raise ScreenError(f"scrcpy 压缩包包含非法上级目录引用：{name}")
    return rel


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    resolved_root = dest.resolve()
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        deferred_links: list[tuple[Path, str]] = []
        for member in members:
            rel = _validate_relative_member(member.name)
            target = dest / rel
            _ensure_within(resolved_root, target.parent, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    raise ScreenError(f"无法读取 scrcpy 压缩包文件：{member.name}")
                with extracted, target.open("wb") as out:
                    shutil.copyfileobj(extracted, out)
                mode = member.mode & 0o777
                if mode and os.name != "nt":
                    os.chmod(target, mode)
                continue
            if member.issym():
                link_target = member.linkname
                if os.path.isabs(link_target):
                    raise ScreenError(f"scrcpy 压缩包包含绝对符号链接，已拒绝：{member.name}")
                resolved_link = (target.parent / link_target).resolve()
                if resolved_link != resolved_root and not resolved_link.is_relative_to(resolved_root):
                    raise ScreenError(f"scrcpy 压缩包包含越界符号链接，已拒绝：{member.name}")
                deferred_links.append((target, link_target))
                continue
            raise ScreenError(f"scrcpy 压缩包包含不支持的条目类型：{member.name}")
        for target, link_target in deferred_links:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(link_target)


def _safe_extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    resolved_root = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            rel = _validate_relative_member(info.filename)
            target = dest / rel
            _ensure_within(resolved_root, target.parent, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Reject symlinks encoded in POSIX zip external attributes.
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == stat.S_IFLNK:
                raise ScreenError(f"scrcpy zip 压缩包包含符号链接，已拒绝：{info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            perms = (info.external_attr >> 16) & 0o777
            if perms and os.name != "nt":
                os.chmod(target, perms)


def _download_and_verify(url: str, dest_file: Path, expected_sha256: str, *, timeout: int = 120) -> str:
    request = Request(url, headers={"User-Agent": "xiaohe-geo-collector"})
    hasher = hashlib.sha256()
    try:
        with urlopen(request, timeout=timeout) as response, dest_file.open("wb") as out:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
                out.write(chunk)
    except (URLError, OSError, TimeoutError) as exc:
        raise ScreenError(
            f"下载 scrcpy 失败（{url}）：{exc}。国内网络可尝试设置 GEO_COLLECT_SCRCPY_MIRROR 或 --mirror-prefix，"
            "或手动安装 scrcpy 后再试。"
        ) from exc
    actual = hasher.hexdigest()
    if actual.lower() != expected_sha256.lower():
        raise ScreenError(
            f"scrcpy 安装包 SHA256 校验失败（期望 {expected_sha256[:12]}...，实际 {actual[:12]}...），已中止安装。"
        )
    return actual


def install_scrcpy(
    *,
    cache_root: Path | None = None,
    download_url: str | None = None,
    mirror_prefix: str | None = None,
    force: bool = False,
    timeout: int = 120,
    system: str | None = None,
    machine: str | None = None,
) -> ScrcpyInstallation:
    """Download, verify SHA256, and atomically extract official scrcpy into managed cache."""
    asset_name, expected_sha256 = platform_asset(system, machine)
    url = build_download_url(asset_name, download_url=download_url, mirror_prefix=mirror_prefix)
    root = scrcpy_cache_root(cache_root)
    install_dir = root / SCRCPY_VERSION

    if not force:
        existing = _locate_managed_binary(install_dir, system=system)
        if existing is not None:
            exe, server = existing
            return ScrcpyInstallation(
                executable=str(exe),
                source="managed",
                version=SCRCPY_VERSION,
                server_path=str(server) if server else None,
                installed_dir=str(install_dir),
            )

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{SCRCPY_VERSION}-", dir=root))
    backup: Path | None = None
    try:
        archive_path = staging / asset_name
        print(
            json.dumps(
                {"event": "scrcpy_downloading", "version": SCRCPY_VERSION, "asset": asset_name, "url": url},
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        _download_and_verify(url, archive_path, expected_sha256, timeout=timeout)
        unpacked = staging / "unpacked"
        if asset_name.endswith(".zip"):
            _safe_extract_zip(archive_path, unpacked)
        else:
            _safe_extract_tar(archive_path, unpacked)
        located = _locate_managed_binary(unpacked, system=system)
        if located is None:
            raise ScreenError(f"scrcpy 安装包中未找到可执行文件：{_binary_name(system)}")
        exe, server = located
        if os.name != "nt":
            os.chmod(exe, 0o755)
        payload_root = exe.parent
        receipt = {
            "owner": "geo-collector",
            "version": SCRCPY_VERSION,
            "asset": asset_name,
            "sha256": expected_sha256,
            "url": url,
        }
        (payload_root / SCRCPY_RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")

        if install_dir.exists():
            if not force:
                existing = _locate_managed_binary(install_dir, system=system)
                if existing is not None:
                    ex_exe, ex_server = existing
                    return ScrcpyInstallation(
                        executable=str(ex_exe),
                        source="managed",
                        version=SCRCPY_VERSION,
                        server_path=str(ex_server) if ex_server else None,
                        installed_dir=str(install_dir),
                    )
            backup = root / f".backup-{SCRCPY_VERSION}-{uuid.uuid4().hex[:8]}"
            install_dir.rename(backup)
        try:
            payload_root.rename(install_dir)
        except OSError:
            if backup is not None and backup.exists() and not install_dir.exists():
                backup.rename(install_dir)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        final_located = _locate_managed_binary(install_dir, system=system)
        if final_located is None:
            raise ScreenError("scrcpy 解压落地后未找到可执行文件。")
        final_exe, final_server = final_located
        return ScrcpyInstallation(
            executable=str(final_exe),
            source="installed",
            version=SCRCPY_VERSION,
            server_path=str(final_server) if final_server else None,
            installed_dir=str(install_dir),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def resolve_scrcpy(
    *,
    scrcpy_path: str | None = None,
    auto_install: bool = True,
    force_install: bool = False,
    cache_root: Path | None = None,
    download_url: str | None = None,
    mirror_prefix: str | None = None,
    timeout: int = 120,
    dry_run: bool = False,
    system: str | None = None,
    machine: str | None = None,
) -> ScrcpyInstallation:
    """Locate an existing scrcpy binary or install the official managed release."""
    explicit = scrcpy_path or os.environ.get("GEO_COLLECT_SCRCPY_PATH")
    if explicit and force_install:
        raise ValueError("已通过 --scrcpy 或 GEO_COLLECT_SCRCPY_PATH 指定路径，不能同时使用 --force-install")
    if explicit:
        candidate = Path(explicit).expanduser()
        is_bare_name = (os.sep not in explicit) and (os.altsep not in explicit if os.altsep else True)
        resolved = str(candidate) if candidate.is_file() else (shutil.which(explicit) if is_bare_name else None)
        if not resolved:
            raise ScreenError(f"指定的 scrcpy 路径不存在或不可执行：{explicit}")
        server = Path(resolved).parent / "scrcpy-server"
        return ScrcpyInstallation(
            executable=resolved,
            source="configured",
            server_path=str(server) if server.is_file() else None,
        )

    root = scrcpy_cache_root(cache_root)
    install_dir = root / SCRCPY_VERSION

    if force_install:
        asset_name, _ = platform_asset(system, machine)
        build_download_url(asset_name, download_url=download_url, mirror_prefix=mirror_prefix)
        if dry_run:
            return ScrcpyInstallation(
                executable=str(install_dir / _binary_name(system)),
                source="would_install",
                version=SCRCPY_VERSION,
                server_path=str(install_dir / "scrcpy-server"),
                installed_dir=str(install_dir),
            )
        return install_scrcpy(
            cache_root=root,
            download_url=download_url,
            mirror_prefix=mirror_prefix,
            force=True,
            timeout=timeout,
            system=system,
            machine=machine,
        )

    system_bin = shutil.which("scrcpy")
    if system_bin:
        return ScrcpyInstallation(executable=system_bin, source="system")

    existing = _locate_managed_binary(install_dir, system=system)
    if existing is not None:
        exe, server = existing
        return ScrcpyInstallation(
            executable=str(exe),
            source="managed",
            version=SCRCPY_VERSION,
            server_path=str(server) if server else None,
            installed_dir=str(install_dir),
        )

    if not auto_install:
        raise ScreenError(
            "未找到 scrcpy，且已禁用自动安装。请安装 scrcpy、通过 --scrcpy 指定路径，或去掉 --no-auto-install 允许自动安装。"
        )

    asset_name, _ = platform_asset(system, machine)
    build_download_url(asset_name, download_url=download_url, mirror_prefix=mirror_prefix)
    if dry_run:
        return ScrcpyInstallation(
            executable=str(install_dir / _binary_name(system)),
            source="would_install",
            version=SCRCPY_VERSION,
            server_path=str(install_dir / "scrcpy-server"),
            installed_dir=str(install_dir),
        )
    return install_scrcpy(
        cache_root=root,
        download_url=download_url,
        mirror_prefix=mirror_prefix,
        force=False,
        timeout=timeout,
        system=system,
        machine=machine,
    )


def safe_serial_slug(serial: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", serial.strip()).strip("_")
    return (cleaned or "device")[:40]


def parse_serials(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    parts = [raw] if isinstance(raw, str) else list(raw)
    result: list[str] = []
    for part in parts:
        for item in str(part).split(","):
            value = item.strip()
            if value and value not in result:
                result.append(value)
    return result


def list_adb_devices(adb: str | None = None) -> tuple[str, list[dict]]:
    """List all ADB devices and their authorization/model metadata without touching uiautomator2."""
    adb_path = _resolve_adb(adb)
    try:
        output = subprocess.run([adb_path, "devices", "-l"], check=True, capture_output=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError("ADB命令失败；请检查设备连接和ADB路径。") from exc
    entries: list[dict] = []
    for line in output.decode("utf-8", errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        meta = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                meta[key] = value
        entries.append({
            "serial": serial,
            "state": state,
            "ready": state == "device",
            "model": meta.get("model", ""),
            "device": meta.get("device", ""),
            "product": meta.get("product", ""),
            "transport_id": meta.get("transport_id", ""),
        })
    return adb_path, entries


def select_adb_device(serial: str | None = None, adb: str | None = None) -> tuple[str, str]:
    """Resolve ADB binary and connected device serial without touching uiautomator2 or locks."""
    adb_path, entries = list_adb_devices(adb)
    devices = {item["serial"]: item["state"] for item in entries}
    ready = [item["serial"] for item in entries if item["ready"]]
    if serial is not None and devices.get(serial) != "device":
        raise DeviceError("指定手机未连接或尚未授权ADB。")
    if serial is None and len(ready) != 1:
        raise DeviceError(f"已授权设备数量为{len(ready)}，请通过--serial明确选择手机。")
    return adb_path, serial or ready[0]


def select_adb_devices(
    *,
    serial: str | None = None,
    serials: str | list[str] | tuple[str, ...] | None = None,
    all_devices: bool = False,
    adb: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve one or multiple ready ADB devices."""
    parsed_serials = parse_serials(serials)
    modes = sum(bool(flag) for flag in (serial, parsed_serials, all_devices))
    if modes > 1:
        raise ValueError("--serial、--serials 与 --all / --all-devices 不能同时使用")
    if not parsed_serials and not all_devices:
        adb_path, single = select_adb_device(serial=serial, adb=adb)
        return adb_path, [single]

    adb_path, entries = list_adb_devices(adb)
    devices = {item["serial"]: item["state"] for item in entries}
    ready = [item["serial"] for item in entries if item["ready"]]
    if all_devices:
        if not ready:
            raise DeviceError("未检测到已授权的 ADB 手机，请检查连接与 USB 调试授权。")
        return adb_path, ready
    missing = [item for item in parsed_serials if devices.get(item) != "device"]
    if missing:
        raise DeviceError(f"以下指定手机未连接或尚未授权ADB：{', '.join(missing)}")
    return adb_path, parsed_serials


def tile_window_layout(
    count: int,
    *,
    window_width: int = 360,
    origin_x: int = 40,
    origin_y: int = 60,
    gap_x: int = 16,
    gap_y: int = 44,
    columns: int | None = None,
) -> list[dict[str, int]]:
    """Compute non-overlapping side-by-side window positions for multi-device mirroring."""
    if count <= 0:
        return []
    if window_width < 120:
        raise ValueError("--window-width 不能小于 120")
    cols = columns if columns is not None else min(count, 4)
    if cols < 1:
        raise ValueError("--columns 不能小于 1")
    row_height = int(window_width * 2.15)
    positions: list[dict[str, int]] = []
    for index in range(count):
        row, col = divmod(index, cols)
        positions.append({
            "window_x": origin_x + col * (window_width + gap_x),
            "window_y": origin_y + row * (row_height + gap_y),
            "window_width": window_width,
        })
    return positions


def build_scrcpy_command(
    installation: ScrcpyInstallation,
    *,
    serial: str,
    control: bool = False,
    audio: bool = False,
    max_size: int = 1080,
    max_fps: int = 30,
    always_on_top: bool = False,
    title: str | None = None,
    window_x: int | None = None,
    window_y: int | None = None,
    window_width: int | None = None,
    window_height: int | None = None,
) -> list[str]:
    if max_size < 0:
        raise ValueError("--max-size 不能小于 0")
    if max_fps < 0:
        raise ValueError("--max-fps 不能小于 0")
    cmd = [installation.executable, "-s", serial]
    if not control:
        cmd.append("--no-control")
    if not audio:
        cmd.append("--no-audio")
    if max_size > 0:
        cmd.append(f"--max-size={max_size}")
    if max_fps > 0:
        cmd.append(f"--max-fps={max_fps}")
    if always_on_top:
        cmd.append("--always-on-top")
    if window_x is not None:
        cmd.append(f"--window-x={window_x}")
    if window_y is not None:
        cmd.append(f"--window-y={window_y}")
    if window_width is not None and window_width > 0:
        cmd.append(f"--window-width={window_width}")
    if window_height is not None and window_height > 0:
        cmd.append(f"--window-height={window_height}")
    window_title = title or f"geo-collect | {serial}"
    cmd.append(f"--window-title={window_title}")
    return cmd


def build_scrcpy_env(
    installation: ScrcpyInstallation,
    *,
    adb_path: str,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    # Pin ADB to the exact binary used by geo-collect so scrcpy's bundled adb
    # cannot restart the adb server and disconnect an active uiautomator2 run.
    env["ADB"] = str(adb_path)
    if installation.server_path:
        env["SCRCPY_SERVER_PATH"] = installation.server_path
    return env


def _read_log_tail(log_path: Path, limit: int = 600) -> str:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return content[-limit:] if content else ""


def _default_log_path(cache_root: Path | None = None, *, serial: str | None = None) -> Path:
    root = scrcpy_cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    if serial:
        return root / f"screen-{safe_serial_slug(serial)}.log"
    return root / "last-screen.log"


def _terminate_process(proc: subprocess.Popen, timeout: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _spawn_scrcpy(
    cmd: list[str],
    env: dict[str, str],
    log_path: Path,
    *,
    detached: bool = False,
    startup_grace: float = 0.5,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {
        "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if detached:
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
    with log_path.open("wb") as log_stream:
        kwargs["stdout"] = log_stream
        kwargs["stderr"] = subprocess.STDOUT
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            raise ScreenError(f"无法启动 scrcpy（{cmd[0]}）：{exc}") from exc
    if startup_grace > 0:
        deadline = time.monotonic() + startup_grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() not in (None, 0):
            detail = _read_log_tail(log_path)
            raise ScreenError(
                f"scrcpy 启动失败（退出码 {proc.returncode}）" + (f"：{detail}" if detail else "")
            )
    return proc


@contextmanager
def screen_mirror(
    *,
    serial: str,
    adb_path: str,
    scrcpy_path: str | None = None,
    control: bool = False,
    audio: bool = False,
    max_size: int = 1080,
    max_fps: int = 30,
    always_on_top: bool = False,
    title: str | None = None,
    window_x: int | None = None,
    window_y: int | None = None,
    window_width: int | None = None,
    window_height: int | None = None,
    auto_install: bool = True,
    download_url: str | None = None,
    mirror_prefix: str | None = None,
    cache_root: Path | None = None,
    log_file: Path | None = None,
    startup_grace: float = 0.5,
):
    """Run scrcpy alongside an active capture session and always close it on exit."""
    installation = resolve_scrcpy(
        scrcpy_path=scrcpy_path,
        auto_install=auto_install,
        cache_root=cache_root,
        download_url=download_url,
        mirror_prefix=mirror_prefix,
    )
    cmd = build_scrcpy_command(
        installation,
        serial=serial,
        control=control,
        audio=audio,
        max_size=max_size,
        max_fps=max_fps,
        always_on_top=always_on_top,
        title=title,
        window_x=window_x,
        window_y=window_y,
        window_width=window_width,
        window_height=window_height,
    )
    env = build_scrcpy_env(installation, adb_path=adb_path)
    log_path = log_file or _default_log_path(cache_root, serial=serial)
    proc = _spawn_scrcpy(cmd, env, log_path, detached=False, startup_grace=startup_grace)
    info = {
        "event": "screen_started",
        "serial": serial,
        "pid": proc.pid,
        "readonly": not control,
        "log_file": str(log_path),
        **installation.to_dict(),
    }
    print(json.dumps(info, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        yield info
    finally:
        _terminate_process(proc)


def run_screen_command(
    *,
    serial: str | None = None,
    serials: str | list[str] | tuple[str, ...] | None = None,
    all_devices: bool = False,
    adb: str | None = None,
    scrcpy_path: str | None = None,
    control: bool = False,
    audio: bool = False,
    max_size: int = 1080,
    max_fps: int = 30,
    always_on_top: bool = False,
    title: str | None = None,
    window_width: int = 360,
    columns: int | None = None,
    background: bool = False,
    auto_install: bool = True,
    install_only: bool = False,
    force_install: bool = False,
    download_url: str | None = None,
    mirror_prefix: str | None = None,
    dry_run: bool = False,
    cache_root: Path | None = None,
    startup_grace: float = 0.5,
) -> dict:
    """Execute the standalone `geo-collect screen` subcommand for one or multiple phones."""
    installation = resolve_scrcpy(
        scrcpy_path=scrcpy_path,
        auto_install=auto_install,
        force_install=force_install,
        cache_root=cache_root,
        download_url=download_url,
        mirror_prefix=mirror_prefix,
        dry_run=dry_run,
    )
    if install_only:
        status = "preview" if dry_run else ("installed" if installation.source == "installed" else "ready")
        return {"status": status, **installation.to_dict()}

    multi_mode = bool(all_devices or parse_serials(serials))
    if not multi_mode:
        adb_path, resolved_serial = select_adb_device(serial=serial, adb=adb)
        cmd = build_scrcpy_command(
            installation,
            serial=resolved_serial,
            control=control,
            audio=audio,
            max_size=max_size,
            max_fps=max_fps,
            always_on_top=always_on_top,
            title=title,
        )
        if dry_run:
            return {
                "status": "preview",
                "serial": resolved_serial,
                "adb": adb_path,
                "readonly": not control,
                "background": background,
                "command": cmd,
                **installation.to_dict(),
            }

        env = build_scrcpy_env(installation, adb_path=adb_path)
        log_path = _default_log_path(cache_root, serial=resolved_serial)
        proc = _spawn_scrcpy(cmd, env, log_path, detached=background, startup_grace=startup_grace)
        base_result = {
            "serial": resolved_serial,
            "adb": adb_path,
            "pid": proc.pid,
            "readonly": not control,
            "command": cmd,
            "log_file": str(log_path),
            **installation.to_dict(),
        }
        if background:
            return {"status": "started", **base_result}

        print(
            json.dumps({"event": "screen_started", **base_result}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
        interrupted = False
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            interrupted = True
            _terminate_process(proc)
            returncode = 0
        if not interrupted and returncode != 0:
            detail = _read_log_tail(log_path)
            raise ScreenError(f"scrcpy 异常退出（退出码 {returncode}）" + (f"：{detail}" if detail else ""))
        return {"status": "closed", "interrupted": interrupted, "returncode": returncode, **base_result}

    adb_path, target_serials = select_adb_devices(
        serial=serial, serials=serials, all_devices=all_devices, adb=adb
    )
    layout = tile_window_layout(len(target_serials), window_width=window_width, columns=columns)
    planned = []
    for idx, (dev_serial, pos) in enumerate(zip(target_serials, layout), 1):
        win_title = f"{title} [{idx}/{len(target_serials)}] | {dev_serial}" if title else f"geo-collect [{idx}/{len(target_serials)}] | {dev_serial}"
        cmd = build_scrcpy_command(
            installation,
            serial=dev_serial,
            control=control,
            audio=audio,
            max_size=max_size,
            max_fps=max_fps,
            always_on_top=always_on_top,
            title=win_title,
            window_x=pos["window_x"],
            window_y=pos["window_y"],
            window_width=pos["window_width"],
        )
        planned.append({"serial": dev_serial, "window": pos, "command": cmd})

    if dry_run:
        return {
            "status": "preview",
            "count": len(planned),
            "adb": adb_path,
            "readonly": not control,
            "background": background,
            "mirrors": planned,
            **installation.to_dict(),
        }

    env = build_scrcpy_env(installation, adb_path=adb_path)
    spawned: list[tuple[dict, subprocess.Popen, Path]] = []
    try:
        for item in planned:
            log_path = _default_log_path(cache_root, serial=item["serial"])
            proc = _spawn_scrcpy(item["command"], env, log_path, detached=background, startup_grace=startup_grace)
            spawned.append((item, proc, log_path))
    except BaseException:
        for _, proc, _ in spawned:
            _terminate_process(proc)
        raise

    mirrors_info = [
        {
            "serial": item["serial"],
            "pid": proc.pid,
            "window": item["window"],
            "command": item["command"],
            "log_file": str(log_path),
        }
        for item, proc, log_path in spawned
    ]
    if background:
        return {
            "status": "started",
            "count": len(mirrors_info),
            "adb": adb_path,
            "readonly": not control,
            "mirrors": mirrors_info,
            **installation.to_dict(),
        }

    print(
        json.dumps(
            {"event": "screens_started", "count": len(mirrors_info), "readonly": not control, "mirrors": mirrors_info},
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )
    interrupted = False
    try:
        for _, proc, _ in spawned:
            proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        for _, proc, _ in spawned:
            _terminate_process(proc)

    failures = []
    for info, (_, proc, log_path) in zip(mirrors_info, spawned):
        rc = 0 if interrupted else (proc.returncode if proc.returncode is not None else 0)
        info["returncode"] = rc
        if not interrupted and rc != 0:
            detail = _read_log_tail(log_path)
            failures.append(f"{info['serial']} (rc={rc})" + (f": {detail}" if detail else ""))
    if failures:
        raise ScreenError("部分 scrcpy 窗口异常退出：" + "; ".join(failures))
    return {
        "status": "closed",
        "interrupted": interrupted,
        "count": len(mirrors_info),
        "adb": adb_path,
        "readonly": not control,
        "mirrors": mirrors_info,
        **installation.to_dict(),
    }
