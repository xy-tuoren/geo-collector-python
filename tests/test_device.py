import io
from types import SimpleNamespace

from PIL import Image
import pytest

from geo_collector import device as module
from geo_collector.device import Device, DeviceError, Observation, logical_to_physical, map_bounds, parse_nodes, physical_to_logical, validate_geometry


PACKAGE = "com.example.capture"


def png(size=(720, 1600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


def xml(package=PACKAGE):
    return f'''<hierarchy rotation="0"><node package="{package}" class="android.widget.FrameLayout" bounds="[0,0][720,1600]" scrollable="true"><node package="{package}" text="参考资料" content-desc="展开资料" resource-id="card_expand" class="android.widget.Button" bounds="[20,180][680,260]" clickable="true"/><node package="{package}" bounds="[10,10][20,20]" visible-to-user="false"/></node></hierarchy>'''


class FakeU2:
    def __init__(self):
        self.info = {"displayWidth": 720, "displayHeight": 1600, "displayRotation": 0, "screenOn": True}
        self.package = PACKAGE
        self.trust = "deviceLocked=false"
        self.policy = "mShowingLockscreen=false"
        self.window_extra = ""
        self.xml = xml()
        self.calls = []
        self.settings = {}
        self.jsonrpc = SimpleNamespace(setConfigurator=lambda config: None)
        self.ime = "original.ime/Keyboard"
        self.send_error = None

    def shell(self, args, timeout=10):
        if args == ["dumpsys", "trust"]:
            output = self.trust
        elif args == ["dumpsys", "window", "policy"]:
            output = self.policy
        elif args == ["dumpsys", "window", "windows"]:
            output = f"mCurrentFocus=Window{{abc u0 {self.package}/.MainActivity}}\n  Window #0 Window{{abc u0 {self.package}/.MainActivity}}:\n    {self.window_extra}"
        elif args[:2] == ["ime", "set"]:
            self.ime = args[2]
            self.calls.append(("restore_ime", args[2]))
            output = ""
        else:
            raise AssertionError(args)
        return SimpleNamespace(output=output, exit_code=0)

    def dump_hierarchy(self, **kwargs):
        return self.xml

    def app_info(self, package):
        return {"versionName": "1.2.3"}

    def current_ime(self):
        return self.ime

    def set_input_ime(self, enabled):
        self.ime = "fast.input/IME"
        self.calls.append(("input_ime", enabled))

    def send_keys(self, text, clear=True):
        self.calls.append(("send_keys", text, clear))
        if self.send_error:
            raise self.send_error

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def swipe(self, *args, duration):
        self.calls.append(("swipe", *args, duration))

    def press(self, key):
        self.calls.append(("press", key))

    def app_stop(self, package):
        self.calls.append(("stop", package))

    def app_start(self, package, **kwargs):
        self.calls.append(("start", package, kwargs))
        self.package = package

    def app_wait(self, package, **kwargs):
        return 1


@pytest.fixture
def connected(monkeypatch):
    fake = FakeU2()
    commands = []
    frames = {"png": png()}

    def adb_run(self, *args):
        commands.append(args)
        if args == ("devices", "-l"):
            return b"List of devices attached\nphone-1 device model:Test\n"
        assert args == ("-s", "phone-1", "exec-out", "screencap", "-p")
        return frames["png"]

    monkeypatch.setattr(Device, "_adb", adb_run)
    monkeypatch.setattr(module.u2, "connect", lambda serial: fake)
    device = Device()
    yield device, fake, commands, frames
    device.close()


@pytest.mark.parametrize("physical,logical", [((720, 1600), (720, 1600)), ((1080, 2400), (720, 1600))])
def test_coordinate_spaces_and_complete_rectangle_mapping(physical, logical):
    assert physical_to_logical((physical[0] / 2, physical[1] / 2), physical, logical) == (360, 800)
    assert logical_to_physical((360, 800), logical, physical) == (physical[0] / 2, physical[1] / 2)
    assert map_bounds((0, 0, logical[0], logical[1]), logical, physical) == (0, 0, *physical)
    with pytest.raises(DeviceError):
        physical_to_logical((physical[0], 0), physical, logical)
    with pytest.raises(DeviceError):
        physical_to_logical((float("nan"), 0), physical, logical)
    with pytest.raises(DeviceError):
        physical_to_logical((True, 0), physical, logical)
    with pytest.raises(DeviceError):
        map_bounds((-1, 0, 500, 500), logical, physical)


def test_unsupported_geometry_and_visible_node_contract():
    for logical, physical, rotation in [((1600, 720), (1600, 720), 1), ((720, 1600), (1080, 1920), 0), ((720, 1600), (720, 1600), 1)]:
        with pytest.raises(DeviceError):
            validate_geometry(logical, physical, rotation)
    nodes = parse_nodes(xml())
    assert len(nodes) == 2
    assert nodes[0]["scrollable"]
    assert nodes[1] == {"text": "参考资料", "description": "展开资料", "resource_id": "card_expand", "class_name": "android.widget.Button", "bounds": (20, 180, 680, 260), "clickable": True, "scrollable": False, "package": PACKAGE, "focused": False, "enabled": True}
    assert parse_nodes(xml().replace('clickable="true"', 'clickable="true" focused="true" enabled="false"'))[1]["focused"] is True
    assert parse_nodes(xml().replace('clickable="true"', 'clickable="true" focused="true" enabled="false"'))[1]["enabled"] is False


def test_foreground_uses_resumed_activity_instead_of_wallpaper():
    windows = """mObscuringWindow=Window{abc u0 com.wallpaper/ImageWallpaper}
  mResumeActivity:ActivityRecord{def u0 com.miui.home/.launcher.Launcher} t2}"""
    assert module._window_foreground(windows) == ("com.miui.home", "com.miui.home.launcher.Launcher")


def test_device_selection_requires_one_authorized_phone(monkeypatch):
    monkeypatch.setattr(Device, "_adb", lambda self, *args: b"List of devices attached\na device\nb device\nc unauthorized\n")
    monkeypatch.setattr(module.u2, "connect", lambda serial: pytest.fail("must not connect"))
    with pytest.raises(DeviceError, match="--serial"):
        Device()
    with pytest.raises(DeviceError, match="授权"):
        Device("c")


def test_resolve_adb_uses_bundled_binary_when_system_adb_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("GEO_COLLECT_ADB_PATH", raising=False)
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    monkeypatch.setattr(module.adbutils, "adb_path", lambda: "bundled-adb")
    assert module._resolve_adb(None) == "bundled-adb"
    assert module._resolve_adb("explicit-adb") == "explicit-adb"


@pytest.mark.parametrize("physical", [(720, 1600), (1080, 2400)])
def test_observe_uses_raw_png_and_ocr_remains_in_physical_space(connected, physical):
    device, fake, commands, frames = connected
    frames["png"] = png(physical)
    device._ocr = lambda data, **kwargs: SimpleNamespace(boxes=[[[30, 60], [90, 60], [90, 100], [30, 100]]], txts=["参考资料"], scores=[0.98])
    observation = device.observe(ocr=True)
    assert observation.logical_size == (720, 1600)
    assert observation.physical_size == physical
    assert observation.png == frames["png"]
    assert observation.package == PACKAGE
    assert observation.activity == PACKAGE + ".MainActivity"
    assert observation.version == "1.2.3"
    assert observation.texts == [{"text": "参考资料", "confidence": 0.98, "bounds": (30, 60, 90, 100)}]
    assert fake.calls == []
    assert commands[-1] == ("-s", "phone-1", "exec-out", "screencap", "-p")


@pytest.mark.parametrize("recovers", [False, True])
def test_transient_toast_hierarchy_retries_only_observation(connected, recovers):
    device, fake, _, _ = connected
    dumps = []
    def hierarchy(**kwargs):
        dumps.append(1)
        return xml(PACKAGE if recovers and len(dumps) == 3 else "android.toast")
    fake.dump_hierarchy = hierarchy
    if recovers:
        assert device.observe().package == PACKAGE
    else:
        with pytest.raises(DeviceError, match="层级不属于"):
            device.observe()
    assert len(dumps) == 3 and fake.calls == []


def test_actions_use_u2_only_and_restore_ime_without_replaying_uncertain_input(connected):
    device, fake, commands, _ = connected
    device.observe()
    device.tap((360, 220), PACKAGE)
    device.swipe((360, 1300), (360, 500), expected_package=PACKAGE)
    device.press("back", PACKAGE)
    device.input_text("本地测试文本", PACKAGE)
    assert fake.calls[:3] == [("click", 360, 220), ("swipe", 360, 1300, 360, 500, 0.4), ("press", "back")]
    assert fake.ime == "original.ime/Keyboard"
    fake.send_error = RuntimeError("uncertain input")
    with pytest.raises(RuntimeError, match="uncertain"):
        device.input_text("下一条测试文本", PACKAGE)
    assert fake.ime == "original.ime/Keyboard"
    assert len([call for call in fake.calls if call[0] == "send_keys"]) == 2
    assert all("input" not in command for command in commands)


@pytest.mark.parametrize("problem", ["wrong_app", "locked", "keyguard", "unknown_lock", "screen_off", "rotation", "split", "missing_package"])
def test_action_guards_stop_before_any_effect(connected, problem):
    device, fake, _, _ = connected
    expected = PACKAGE
    if problem == "wrong_app":
        fake.package = "com.example.other"
    elif problem == "locked":
        fake.trust = "deviceLocked=true"
    elif problem == "keyguard":
        fake.policy = "mShowingLockscreen=true"
    elif problem == "unknown_lock":
        fake.trust = fake.policy = ""
    elif problem == "screen_off":
        fake.info["screenOn"] = False
    elif problem == "rotation":
        fake.info["displayRotation"] = 1
    elif problem == "split":
        fake.window_extra = "mWindowingMode=3"
    elif problem == "missing_package":
        expected = None
    with pytest.raises(DeviceError):
        device.tap((360, 800), expected)
    assert fake.calls == []


def test_stale_dimensions_and_wrong_hierarchy_are_rejected(connected):
    device, fake, _, frames = connected
    device.observe()
    fake.info["displayWidth"], fake.info["displayHeight"] = (1080, 2400)
    frames["png"] = png((1080, 2400))
    with pytest.raises(DeviceError, match="重新观察"):
        device.tap((360, 800), PACKAGE)
    assert fake.calls == []
    fake.xml = xml("com.example.other")
    with pytest.raises(DeviceError, match="层级"):
        device.observe()


def test_launch_is_explicit_and_observation_handle_can_be_closed(connected):
    device, fake, _, _ = connected
    device.launch(PACKAGE, restart=True)
    assert fake.calls == [("stop", PACKAGE), ("start", PACKAGE, {"wait": False, "use_monkey": False})]
    device.close()
    with pytest.raises(DeviceError, match="关闭"):
        device.screenshot()


def test_launch_foreground_timeout_is_distinct_from_other_device_errors(connected, monkeypatch):
    device, fake, _, _ = connected
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    start = fake.app_start

    def stay_in_other_app(package, **kwargs):
        start(package, **kwargs)
        fake.package = "com.example.other"

    monkeypatch.setattr(fake, "app_start", stay_in_other_app)
    with pytest.raises(module.LaunchForegroundError):
        device.launch(PACKAGE, restart=True)
    assert clock[0] == 20.0
    assert fake.calls == [("stop", PACKAGE), ("start", PACKAGE, {"wait": False, "use_monkey": False})]


def test_launch_waits_on_real_foreground_without_relaunching_or_relying_on_pid(connected, monkeypatch):
    device, fake, _, _ = connected
    clock, after_start = [0.0], [False]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    start, foreground = fake.app_start, device.foreground

    def launch_once(package, **kwargs):
        start(package, **kwargs)
        after_start[0] = True

    def delayed_window():
        if after_start[0] and clock[0] < 1:
            raise module.ForegroundUnavailable("无法确认系统当前前台应用。")
        if after_start[0] and clock[0] < 12:
            return "com.example.other", "com.example.other.MainActivity"
        return foreground()

    monkeypatch.setattr(fake, "app_start", launch_once)
    monkeypatch.setattr(device, "foreground", delayed_window)
    monkeypatch.setattr(fake, "app_wait", lambda *args, **kwargs: pytest.fail("must not use PID wait"))
    device.launch(PACKAGE)
    assert clock[0] == 12.0
    assert fake.calls == [("start", PACKAGE, {"wait": False, "use_monkey": False})]


def test_launch_does_not_swallow_device_connection_errors(connected, monkeypatch):
    device, fake, _, _ = connected
    start = fake.app_start

    def start_then_disconnect(package, **kwargs):
        start(package, **kwargs)
        monkeypatch.setattr(device, "foreground", lambda: (_ for _ in ()).throw(DeviceError("设备连接已关闭。")))

    monkeypatch.setattr(fake, "app_start", start_then_disconnect)
    with pytest.raises(DeviceError, match="设备连接已关闭"):
        device.launch(PACKAGE)
    assert len(fake.calls) == 1


def test_malformed_ocr_cannot_silently_drop_required_boxes(connected):
    device, _, _, _ = connected
    observation = device.observe()
    device._ocr = lambda *args, **kwargs: SimpleNamespace(txts=["资料"], boxes=None, scores=[0.9])
    with pytest.raises(DeviceError, match="OCR"):
        device.recognize(observation)


def test_multi_slot_ocr_cache_reuses_alternating_frames(connected):
    device, _, _, frames = connected
    ocr_calls = []
    device._ocr = lambda data, **kwargs: (
        ocr_calls.append(len(data)),
        SimpleNamespace(boxes=[[[10, 10], [80, 10], [80, 40], [10, 40]]], txts=["ok"], scores=[0.99]),
    )[1]
    img_a = png((720, 1600))
    buf = io.BytesIO()
    Image.new("RGB", (720, 1600), "black").save(buf, "PNG")
    img_b = buf.getvalue()
    frames["png"] = img_a
    obs_a1 = device.observe(ocr=True)
    frames["png"] = img_b
    obs_b1 = device.observe(ocr=True)
    frames["png"] = img_a
    obs_a2 = device.observe(ocr=True)
    assert len(ocr_calls) == 2
    assert obs_a1.texts == obs_a2.texts and obs_b1.texts == obs_a1.texts


def test_compound_state_and_fast_guard_skip_redundant_screencap(connected):
    device, fake, commands, frames = connected
    shell_calls = []

    class FastAdb:
        def shell(self, cmd, encoding=None):
            assert cmd == "screencap -p" and encoding is None
            return frames["png"]

    def compound_shell(args, timeout=10):
        shell_calls.append(args)
        assert args[:2] == ["sh", "-c"]
        out = module._STATE_SEP.join([
            fake.trust,
            fake.policy,
            f"mCurrentFocus=Window{{abc u0 {fake.package}/.MainActivity}}",
            "Physical size: 720x1600",
        ])
        return SimpleNamespace(output=out, exit_code=0)

    fake.shell = compound_shell
    device._adb_device = FastAdb()
    device._fast_screencap = True
    device._compound_state = True
    commands.clear()

    device.observe()
    before_shells = len(shell_calls)
    device.tap((360, 400), PACKAGE)
    # _guard used 1 compound shell and 0 subprocess adb screencaps!
    assert len(shell_calls) == before_shells + 1
    assert commands == []
    assert fake.calls[-1] == ("click", 360, 400)


def test_launch_retries_transient_foreground_unavailable_before_guard(connected, monkeypatch):
    device, fake, _, _ = connected
    clock, attempts = [0.0], [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda d: clock.__setitem__(0, clock[0] + d))
    real_fg = device.foreground

    def flaky_fg():
        attempts[0] += 1
        if attempts[0] == 1:
            raise module.ForegroundUnavailable("transient")
        return real_fg()

    monkeypatch.setattr(device, "foreground", flaky_fg)
    device.launch(PACKAGE)
    assert attempts[0] >= 2 and fake.calls[-1][0] == "start"
