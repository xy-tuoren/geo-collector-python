from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from geo_collector.images import CaptureError
from geo_collector.rules import RuleStore, apply_layout
from geo_collector.capture import answer_viewport, _product_images_ready
from geo_collector.recovery import NeedsRepair, submit_repair
from geo_collector.storage import read_json
from test_capture import FakeDevice, fast_capture, PACKAGE
from test_device import connected
from test_recovery import engine as make_engine, FakeDevice as RepairDevice, recipe


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_settle_uses_png_probes_and_one_full_observation(scale, logical_scale):
    phone = FakeDevice(scale, logical_scale)
    item = phone.observe()
    capture, engine = fast_capture(phone)
    capture.package = PACKAGE
    capture.viewport = (0, 80 * scale, 360 * scale, 680 * scale)
    full, shots = [], []
    phone.observe = lambda **kw: (full.append(1), item)[1]
    phone.screenshot = lambda: (shots.append(1), item.png)[1]
    assert capture._settled().png == item.png
    assert len(full) == 1 and len(shots) == 2
    assert engine.events[-1][0] == "frame_settled"


def test_settle_rechecks_foreground_after_quiet_pixels():
    phone = FakeDevice()
    item = phone.observe()
    capture, _ = fast_capture(phone)
    capture.package, capture.viewport = PACKAGE, (0, 80, 360, 680)
    phone.screenshot = lambda: item.png
    phone.observe = lambda **kw: replace(item, package="other.app")
    with pytest.raises(CaptureError, match="前台"):
        capture._settled()


def test_douyin_feed_is_rejected_before_any_scroll():
    phone = FakeDevice()
    phone.package = "com.ss.android.ugc.aweme"
    capture, _ = fast_capture(phone)
    capture.package = phone.package
    with pytest.raises(CaptureError, match="不是小程序"):
        capture._observe()
    assert not phone.swipes


def test_ocr_reuses_only_identical_png_and_caches_empty_result(connected):
    # Fixture is declared in test_device; see explicit import below.
    device, _, _, frames = connected
    calls = []
    device._ocr = lambda *a, **k: (calls.append(1), SimpleNamespace(txts=None))[1]
    item = device.observe()
    device.recognize(item)
    device.recognize(device.observe())
    assert len(calls) == 1
    from test_device import png
    frames["png"] = png((1080, 2400))
    device.recognize(device.observe())
    assert len(calls) == 2


def test_action_refuses_same_package_activity_changed_after_observation(connected):
    from geo_collector.device import DeviceError
    device, fake, _, _ = connected
    item = device.observe()
    original = fake.shell
    def changed(args, **kwargs):
        result = original(args, **kwargs)
        result.output = result.output.replace(".MainActivity", ".OtherActivity")
        return result
    fake.shell = changed
    with pytest.raises(DeviceError, match="页面已切换"):
        device.tap((100, 200), item.package)
    assert not fake.calls


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_layout_candidate_requires_complete_capture_then_replays_without_agent(tmp_path, scale, logical_scale):
    item = FakeDevice(scale, logical_scale).observe()
    item.nodes = [{**item.nodes[0], "resource_id": "new.body", "scrollable": False}]
    machine = make_engine(tmp_path, device=RepairDevice(item))
    def check(o):
        try:
            return answer_viewport(o)
        except CaptureError:
            return None
    with pytest.raises(NeedsRepair) as error:
        machine.layout("answer_container", item, check, request=True)
    file = error.value.request
    plan = {"layout": {"role": "answer_container", "target": {"kind": "node", "attr": "resource_id", "value": "new.body"}}}
    submit_repair(file, plan=plan, sha256=read_json(file)["sha256"])
    assert machine.layout("answer_container", item, check, request=True)
    assert machine.store.get(PACKAGE, "layout.answer_container") is None
    machine.commit_layouts()
    assert read_json(file)["status"] == "verified"
    fresh = make_engine(tmp_path / "next", device=RepairDevice(item), store=machine.store)
    assert fresh.layout("answer_container", item, check, request=True)
    assert fresh.state.get("agent_requests", 0) == 0
    assert fresh.device.actions == []


def test_layout_cannot_hide_unloaded_existing_photo(tmp_path):
    from io import BytesIO
    from PIL import Image
    import numpy as np
    item = FakeDevice().observe()
    picture = Image.new("RGB", (360, 800), "white")
    picture.paste(Image.fromarray(np.random.default_rng(1).integers(0,255,(60,60,3), dtype=np.uint8)), (180, 150))
    data = BytesIO(); picture.save(data, format="PNG")
    item = replace(item, png=data.getvalue(), texts=[], nodes=[
        {"package": PACKAGE, "class_name": "android.widget.ImageView", "bounds": [20, 150, 80, 210]},
        {"package": PACKAGE, "class_name": "vendor.Photo", "resource_id": "photo", "bounds": [180, 150, 240, 210]}])
    plan = {"layout": {"role": "product_images", "target": {"kind":"node", "attr":"resource_id", "value":"photo"}}}
    hinted = apply_layout(plan, item)
    ready, boxes = _product_images_ready(hinted, (0, 100, 360, 700))
    assert len(boxes) == 2 and not ready


def test_rule_variants_keep_distinct_app_versions_and_activities(tmp_path):
    item = FakeDevice().observe()
    store = RuleStore(tmp_path)
    store.promote(PACKAGE, "open", recipe(), item, "agent")
    other = replace(item, version="2", activity="NewActivity")
    store.promote(PACKAGE, "open", recipe("备用入口"), other, "agent")
    assert store.get(PACKAGE, "open", item)["revision"] == 1
    assert store.get(PACKAGE, "open", other)["revision"] == 2
    # An unknown app version still tries the same-activity rule instead of discarding it.
    assert store.get(PACKAGE, "open", replace(item, version="unknown"))["revision"] == 1
    assert store.get(PACKAGE, "open", replace(item, activity="Unrelated")) is None
    store.invalidate(PACKAGE, "open", "changed", revision=2, immediate=True)
    assert store.get(PACKAGE, "open", item)["revision"] == 1
    assert store.get(PACKAGE, "open", other) is None


@pytest.mark.parametrize("phase,products,action", [("submitting", False, "retry-current"), ("capture", True, "reenter-answer"), ("capture", True, "restart-app")])
def test_recovery_refuses_uncertain_sends_and_leaving_products(tmp_path, phase, products, action):
    from geo_collector.workflow import recover_current
    state = {"serial":"phone", "tasks":[{"entry":"douyin", "index":1, "text":"question", "phase":phase}]}
    if products:
        state["products_resume"] = {"phase":"list"}
    with pytest.raises(ValueError):
        recover_current(SimpleNamespace(serial="phone"), RuleStore(tmp_path), tmp_path, state, action, "test")
    assert state["tasks"][0]["phase"] == phase


def test_body_resume_verifies_interrupted_scroll_without_repeating_generation_or_top(tmp_path, monkeypatch):
    import geo_collector.capture as module
    phone = FakeDevice(references=False)
    capture, engine = fast_capture(phone)
    original = module.match_overlap
    from geo_collector.storage import write_json
    engine.save = lambda: write_json(tmp_path / "run.json", engine.state)
    def interrupted(*args, **kwargs):
        raise CaptureError("interrupted seam")
    monkeypatch.setattr(module, "match_overlap", interrupted)
    with pytest.raises(CaptureError, match="interrupted seam"):
        capture.run(phone.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert len(engine.state["body_resume"]["frame_files"]) == 1
    assert not (tmp_path / "delivery").exists()
    monkeypatch.setattr(module, "match_overlap", original)
    def forbidden(*args):
        raise AssertionError("resume repeated generation/top traversal")
    capture._complete = capture._top = forbidden
    result = capture.run(phone.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["end_confirmed"] and "body_resume" not in engine.state
    assert any(name == "answer_body_resumed" for name, _ in engine.events)


def test_existing_drawer_is_closed_before_any_body_swipe():
    from test_capture import DrawerDevice
    phone = DrawerDevice()
    phone.drawer = True
    capture, engine = fast_capture(phone)
    capture.package = PACKAGE
    result = capture._close_existing_drawer(phone.observe(ocr=True))
    assert not phone.drawer and not phone.swipes
    assert engine.calls[0][0] == "capture.close_existing_products"


@pytest.mark.parametrize("height", [600, 1600])
def test_stride_only_grows_after_two_strong_seams(height):
    capture, _ = fast_capture(FakeDevice())
    capture.viewport = (0, 0, 360, height)
    seam = {"verified":True, "score":1., "shift":height*.43, "overlap":height*.57}
    assert capture._stride([]) == capture._stride([seam]) == .43
    assert .43 < capture._stride([seam, seam]) <= .56
    assert capture._stride([seam, {**seam,"score":.9}]) == .43


def test_long_medicine_list_keeps_every_pixel_with_adaptive_stride(tmp_path):
    from test_capture import DrawerDevice
    from geo_collector.images import image_size
    phone = DrawerDevice(product_height=5500)
    capture, _ = fast_capture(phone)
    result = capture.run(phone.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    products = result["products"]
    assert products["frame_count"] > 10 and products["end_probes"] >= 2
    assert phone.product_offset == 5500 - 650
    assert max(start[1]-end[1] for start,end,expanded in phone.drawer_swipes if expanded) > 650*.43
    assert sum(image_size(Path(p).read_bytes())[1] for p in products["image_paths"]) == 5500 + 45


def test_layout_check_can_resume_promotion_in_a_different_process(tmp_path):
    from geo_collector.recovery import StepEngine
    scene = FakeDevice().observe()
    scene.nodes = [{**scene.nodes[0], 'resource_id': 'body', 'scrollable': False}]
    engine = make_engine(tmp_path, device=RepairDevice(scene))
    def check(item):
        return next((n for n in item.nodes if n.get('scrollable')), None)
    with pytest.raises(NeedsRepair) as stopped:
        engine.layout('answer_container', scene, check, request=True)
    file = Path(stopped.value.request)
    plan = {'layout': {'role': 'answer_container', 'target': {'kind': 'node', 'attr': 'resource_id', 'value': 'body'}}}
    submit_repair(file, plan=plan, sha256=read_json(file)['sha256'])
    assert engine.layout('answer_container', scene, check, request=True)
    restarted = StepEngine(None, engine.store, engine.run_dir, read_json(engine.run_dir/'调试产物'/'run.json'))
    restarted.commit_layouts()
    assert read_json(file)['status'] == 'verified' and engine.store.get(PACKAGE, 'layout.answer_container')
