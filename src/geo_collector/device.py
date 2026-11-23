"""Direct uiautomator2 actions, raw ADB PNG observations and local OCR."""
from __future__ import annotations

import io
from copy import deepcopy
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import adbutils
from PIL import Image
import uiautomator2 as u2


class DeviceError(RuntimeError):
    """An observation or action cannot be performed safely."""


class LaunchForegroundError(DeviceError):
    """Launch was dispatched, but the foreground did not settle in time."""


class ForegroundUnavailable(DeviceError):
    """Android currently exposes no identifiable foreground window."""


class ObservationChanged(DeviceError):
    """Read-only snapshot crossed an app transition or a transient toast."""


@dataclass
class Observation:
    png: bytes
    xml: str
    package: str
    activity: str
    logical_size: tuple[int, int]
    physical_size: tuple[int, int]
    version: str
    nodes: list[dict] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)
    reset_welcome: str | None = None  # Collector-derived receipt for this task’s observed reset.


def validate_geometry(logical_size, physical_size, rotation=0):
    for size in (logical_size, physical_size):
        if (len(size) != 2 or any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in size)
                or size[0] >= size[1]):
            raise DeviceError("仅支持尺寸有效的正常竖屏，横屏或异常视口已停止。")
    if rotation not in (0, 2):
        raise DeviceError("设备旋转不是正常竖屏，已停止。")
    ratio = (physical_size[0] / physical_size[1]) / (logical_size[0] / logical_size[1])
    if abs(ratio - 1) > 0.02:
        raise DeviceError("截图与UI窗口宽高比不匹配，不能可靠映射坐标。")


def map_point(point, source_size, target_size):
    """Map pixel coordinates without treating values below one as percentages."""
    validate_geometry(source_size, target_size)
    if (len(point) != 2 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point)
            or any(n < 0 or n >= size for n, size in zip(point, source_size))):
        raise DeviceError("坐标不在有效视口内，已拒绝操作。")
    return tuple(n * target / source for n, source, target in zip(point, source_size, target_size))


def physical_to_logical(point, physical_size, logical_size):
    return map_point(point, physical_size, logical_size)


def logical_to_physical(point, logical_size, physical_size):
    return map_point(point, logical_size, physical_size)


def map_bounds(bounds, source_size, target_size):
    """Map a rectangle, preserving both its first and last source pixels."""
    validate_geometry(source_size, target_size)
    if (len(bounds) != 4 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in bounds)
            or not (0 <= bounds[0] < bounds[2] <= source_size[0] and 0 <= bounds[1] < bounds[3] <= source_size[1])):
        raise DeviceError("矩形不在有效视口内。")
    sx, sy = target_size[0] / source_size[0], target_size[1] / source_size[1]
    return (math.floor(bounds[0] * sx), math.floor(bounds[1] * sy),
            math.ceil(bounds[2] * sx), math.ceil(bounds[3] * sy))


def parse_nodes(xml: str) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DeviceError("UI层级XML无效。") from exc
    nodes = []
    for element in root.iter("node"):
        attributes = element.attrib
        if attributes.get("visible-to-user") == "false":
            continue
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", attributes.get("bounds", ""))
        if not match:
            continue
        bounds = tuple(map(int, match.groups()))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        nodes.append({"text": attributes.get("text", ""), "description": attributes.get("content-desc", ""),
                      "resource_id": attributes.get("resource-id", ""), "class_name": attributes.get("class", ""),
                      "bounds": bounds, "clickable": attributes.get("clickable") == "true",
                      "scrollable": attributes.get("scrollable") == "true", "package": attributes.get("package", ""),
                      "focused": attributes.get("focused") == "true", "enabled": attributes.get("enabled", "true") == "true"})
    return nodes


def _png_size(png: bytes) -> tuple[int, int]:
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DeviceError("ADB没有返回原始PNG截图。")
    try:
        with Image.open(io.BytesIO(png)) as image:
            size = image.size
            image.verify()
    except (OSError, ValueError) as exc:
        raise DeviceError("ADB截图无法解码。") from exc
    return size


def _logical_geometry(info):
    size = (info.get("displayWidth"), info.get("displayHeight"))
    rotation = info.get("displayRotation")
    validate_geometry(size, size, rotation)
    return size, rotation


def _window_foreground(windows):
    match = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)", windows)
    if not match:
        match = re.search(r"mResumeActivity:.*?\su\d+\s+([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)", windows)
    if not match:
        match = re.search(r"^\s*Window #\d+ Window\{[^\n]*?\su\d+\s+([A-Za-z0-9._]+)/([A-Za-z0-9._$]*Activity[A-Za-z0-9._$]*)\}:", windows, re.MULTILINE)
    if not match:
        raise ForegroundUnavailable("无法确认系统当前前台应用。")
    package, activity = match.groups()
    return package, package + activity if activity.startswith(".") else activity


def _locked(trust, policy):
    trust_match = re.search(r"\bdeviceLocked=(true|false|1|0)\b", trust, re.IGNORECASE)
    trust_locked = trust_match.group(1).lower() in ("true", "1") if trust_match else None
    for pattern in (r"\bmShowingLockscreen=(true|false)\b", r"KeyguardServiceDelegate[\s\S]*?\bshowing=(true|false)\b", r"\bmIsShowing=(true|false)\b"):
        match = re.search(pattern, policy, re.IGNORECASE)
        if match:
            policy_locked = match.group(1).lower() == "true"
            return policy_locked or trust_locked if trust_locked is not None else policy_locked
    return trust_locked


def _resolve_adb(adb):
    configured = adb or os.environ.get("GEO_COLLECT_ADB_PATH") or shutil.which("adb")
    if configured:
        return str(configured)
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    sdk = Path(sdk) if sdk else Path.home() / "Library/Android/sdk"
    candidate = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    if candidate.is_file():
        return str(candidate)
    try:
        return adbutils.adb_path()
    except adbutils.AdbError as error:
        raise DeviceError("找不到 ADB。请安装 Android Platform-Tools 并把 adb 加入 PATH，或用 --adb 指定路径。") from error


class Device:
    def __init__(self, serial=None, adb=None):
        self.adb = _resolve_adb(adb)
        lines = self._adb("devices", "-l").decode("utf-8", errors="replace").splitlines()[1:]
        devices = {parts[0]: parts[1] for line in lines if len(parts := line.split()) >= 2}
        ready = [item for item, state in devices.items() if state == "device"]
        if serial is not None and devices.get(serial) != "device":
            raise DeviceError("指定手机未连接或尚未授权ADB。")
        if serial is None and len(ready) != 1:
            raise DeviceError(f"已授权设备数量为{len(ready)}，请通过--serial明确选择手机。")
        self.serial = serial or ready[0]
        self._device = u2.connect(self.serial)
        self._device.settings["wait_timeout"] = 2.0
        self._device.settings["max_depth"] = 70
        self._device.jsonrpc.setConfigurator({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
        self._ocr = None
        self._ocr_cache = None
        self._versions = {}
        self._last_geometry = None
        self._last_window = None
        self._closed = False

    def _adb(self, *args):
        try:
            return subprocess.run([self.adb, *args], check=True, capture_output=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeviceError("ADB命令失败；请检查设备连接和ADB路径。") from exc

    def _open(self):
        if self._closed:
            raise DeviceError("设备连接已关闭。")

    def _shell(self, *args):
        result = self._device.shell(list(args), timeout=10)
        if getattr(result, "exit_code", 0) != 0:
            raise DeviceError("无法读取Android系统状态。")
        return str(getattr(result, "output", result) or "")

    def foreground(self):
        self._open()
        return _window_foreground(self._shell("dumpsys", "window", "windows"))

    def screenshot(self):
        self._open()
        png = self._adb("-s", self.serial, "exec-out", "screencap", "-p")
        _png_size(png)
        return png

    def _state(self, expected_package=None):
        self._open()
        info = self._device.info
        logical_size, rotation = _logical_geometry(info)
        if not info.get("screenOn"):
            raise DeviceError("手机屏幕未点亮；请解锁后继续。")
        locked = _locked(self._shell("dumpsys", "trust"), self._shell("dumpsys", "window", "policy"))
        if locked is not False:
            raise DeviceError("手机仍被锁定或无法证明已解锁，已停止UI操作。")
        windows = self._shell("dumpsys", "window", "windows")
        package, activity = _window_foreground(windows)
        for block in re.split(r"(?=\s*Window #\d+ Window\{)", windows):
            if package + "/" in block and re.search(r"(?:mWindowingMode|windowingMode)=(?:[3456]\b|split|freeform|multi-window)", block):
                raise DeviceError("前台应用处于分屏或浮窗布局，已停止UI操作。")
        if expected_package is not None and package != expected_package:
            raise DeviceError(f"前台应用不匹配：预期{expected_package}，实际{package}；未执行操作。")
        return logical_size, rotation, package, activity

    def observe(self, ocr=False):
        for attempt in range(3):
            try:
                return self._observe_once(ocr)
            except ObservationChanged:
                if attempt == 2:
                    raise
                time.sleep(.2)

    def _observe_once(self, ocr=False):
        logical_size, rotation, package, activity = self._state()
        xml = self._device.dump_hierarchy(compressed=False, pretty=False, max_depth=70)
        png = self.screenshot()
        physical_size = _png_size(png)
        validate_geometry(logical_size, physical_size, rotation)
        nodes = parse_nodes(xml)
        if not any(node["package"] == package for node in nodes):
            raise ObservationChanged("UI层级不属于前台应用，已拒绝混用不同页面的观察。")
        after_size, after_rotation, after_package, after_activity = self._state()
        if (logical_size, rotation, package, activity) != (after_size, after_rotation, after_package, after_activity):
            raise ObservationChanged("读取截图和层级期间设备页面或尺寸发生变化，请重新观察。")
        if package not in self._versions:
            self._versions[package] = str(self._device.app_info(package).get("versionName") or "unknown")
        observation = Observation(png, xml, package, activity, logical_size, physical_size, self._versions[package], nodes)
        self._last_geometry = (logical_size, physical_size, rotation)
        self._last_window = (package, activity)
        if ocr:
            self.recognize(observation)
        return observation

    def recognize(self, observation):
        self._open()
        if _png_size(observation.png) != observation.physical_size:
            raise DeviceError("OCR截图尺寸与观察记录不一致。")
        if self._ocr_cache is not None and self._ocr_cache[0] == observation.png:
            observation.texts = deepcopy(self._ocr_cache[1])
            return observation.texts
        if self._ocr is None:
            from rapidocr import RapidOCR
            from rapidocr.utils.log import logger
            logger.setLevel(logging.WARNING)
            for handler in logger.handlers:
                handler.setLevel(logging.WARNING)
            self._ocr = RapidOCR()
        result = self._ocr(observation.png, use_det=True, use_cls=False, use_rec=True, text_score=0.5)
        boxes, texts, scores = (getattr(result, key, None) for key in ("boxes", "txts", "scores"))
        observation.texts = []
        if texts is None:
            self._ocr_cache = (observation.png, [])
            return observation.texts
        if boxes is None or scores is None or not len(boxes) == len(texts) == len(scores):
            raise DeviceError("OCR文本与定位结果不完整。")
        width, height = observation.physical_size
        for box, text, score in zip(boxes, texts, scores):
            confidence = float(score)
            coordinates = [(float(point[0]), float(point[1])) for point in box]
            if (not math.isfinite(confidence) or confidence < 0.5 or confidence > 1 or not coordinates
                    or any(not math.isfinite(value) for point in coordinates for value in point)):
                continue
            xs, ys = zip(*coordinates)
            bounds = (max(0, math.floor(min(xs))), max(0, math.floor(min(ys))), min(width, math.ceil(max(xs))), min(height, math.ceil(max(ys))))
            if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
                observation.texts.append({"text": str(text), "confidence": confidence, "bounds": bounds})
        self._ocr_cache = (observation.png, deepcopy(observation.texts))
        return observation.texts

    def _guard(self, expected_package):
        if not isinstance(expected_package, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", expected_package):
            raise DeviceError("必须明确提供预期应用包名。")
        logical_size, rotation, package, activity = self._state(expected_package)
        if self._last_window is not None and self._last_window != (package, activity):
            raise DeviceError("同一应用的页面已切换，请重新观察再操作。")
        physical_size = _png_size(self.screenshot())
        validate_geometry(logical_size, physical_size, rotation)
        geometry = (logical_size, physical_size, rotation)
        if self._last_geometry is not None and self._last_geometry != geometry:
            raise DeviceError("设备尺寸或旋转已变化，请重新观察再定位，未执行操作。")
        # Recheck focus after the screenshot; never use ADB as an input fallback.
        if self.foreground() != (package, activity):
            raise DeviceError("操作前前台应用发生变化，未执行操作。")
        return logical_size

    @staticmethod
    def _action_point(point, size):
        mapped = map_point(point, size, size)
        rounded = tuple(round(value) for value in mapped)
        if any(value >= limit for value, limit in zip(rounded, size)):
            raise DeviceError("舍入后的操作坐标超出窗口。")
        return rounded

    def launch(self, package, restart=False):
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package):
            raise DeviceError("应用包名无效。")
        self._guard(self.foreground()[0])
        if restart:
            self._device.app_stop(package)
        deadline = time.monotonic() + 20.0
        # u2's wait checks a process ID and may time out even when Android has
        # opened the target. Dispatch once and wait on the actual window instead.
        self._device.app_start(package, wait=False, use_monkey=False)
        while True:
            try:
                if self.foreground()[0] == package:
                    return
            except ForegroundUnavailable:
                pass  # Window focus may briefly be empty during app switching.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LaunchForegroundError("应用启动后前台不匹配，未执行后续操作。")
            time.sleep(min(.5, remaining))

    def tap(self, point, expected_package):
        size = self._guard(expected_package)
        self._device.click(*self._action_point(point, size))

    def input_text(self, text, expected_package, clear=True):
        if not isinstance(text, str) or not text or not isinstance(clear, bool):
            raise DeviceError("输入文本不能为空，clear必须是布尔值。")
        self._guard(expected_package)
        original_ime = self._device.current_ime()
        primary_error = None
        try:
            self._device.set_input_ime(True)
            if self.foreground()[0] != expected_package:
                raise DeviceError("输入法准备后前台应用发生变化，未输入文本。")
            self._device.send_keys(text, clear=clear)
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                if original_ime and self._device.current_ime() != original_ime:
                    self._shell("ime", "set", original_ime)
            except Exception as exc:
                if primary_error:
                    primary_error.add_note("恢复原输入法失败；输入操作没有重放。")
                else:
                    raise DeviceError("文本输入已执行，但恢复原输入法失败；不要重复输入。") from exc

    def press(self, key, expected_package):
        if key not in {"back", "enter", "home", "recent", "search", "delete", "menu"}:
            raise DeviceError("不支持此导航按键。")
        self._guard(expected_package)
        self._device.press(key)

    def swipe(self, start, end, duration=0.4, *, expected_package):
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0.05 <= duration <= 5:
            raise DeviceError("滑动时长必须在0.05到5秒之间。")
        size = self._guard(expected_package)
        self._device.swipe(*self._action_point(start, size), *self._action_point(end, size), duration=duration)

    def close(self):
        # uiautomator2's HTTP service is device-owned/shared. Closing this handle
        # must not stop another user's service, app, or screen power state.
        self._closed = True
        self._ocr = None
        self._ocr_cache = None
