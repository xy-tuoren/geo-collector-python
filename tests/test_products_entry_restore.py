"""Restore a real all-medicines entry using saved body pixels, without resending."""
from copy import deepcopy
from io import BytesIO

from PIL import Image, ImageDraw
import pytest

from geo_collector.capture import AnswerCapture, _visible_products_entry, answer_viewport
from geo_collector.images import CaptureError, crop
from geo_collector.recovery import save_observation
from geo_collector.storage import read_json, write_json
from test_capture import FakeDevice, FakeEngine


class EntryPhone(FakeDevice):
    def __init__(self):
        super().__init__(references=False)
        self.entry_label = "全部药品>"

    def observe(self, ocr=False):
        observation = super().observe(ocr)
        top = 1000 - self.offset + 80
        if 80 < top < top + 20 < 680:
            box = (250, top, 345, top + 20)
            observation.texts.append({"text": self.entry_label, "bounds": box, "confidence": .99})
            pixels = Image.open(BytesIO(observation.png)).convert("RGB")
            ImageDraw.Draw(pixels).text((250, top), "ALL MEDICINES >", fill="black")
            data = BytesIO()
            pixels.save(data, "PNG")
            observation.png = data.getvalue()
        return observation


class RestoreEngine(FakeEngine):
    def _close_request(self, path, status):
        request = read_json(path)
        request["status"] = status
        write_json(path, request)
        self.state.pop("pending_request", None)


@pytest.fixture
def recovery(tmp_path):
    phone = EntryPhone()
    engine = RestoreEngine(phone)
    attempt = tmp_path / "capture-original"
    attempt.mkdir()
    phone.offset = 850
    first = phone.observe()
    phone.offset = 1100
    last = phone.observe()
    viewport = answer_viewport(first)
    save_observation(attempt / "001-scroll", first)
    save_observation(attempt / "002-scroll", last)
    files = []
    for number, observation in enumerate((first, last), 1):
        path = attempt / f"frame-{number:03d}.png"
        path.write_bytes(crop(observation.png, viewport))
        files.append(str(path))
    body = {"question": phone.question, "frame_files": files,
            "frame_viewports": [{"viewport_physical": list(viewport)} for _ in files]}
    checkpoint = {"phase": "open", "body": body, "frame_files": [], "seams": []}
    pending = tmp_path / "request.json"
    write_json(pending, {"step": "products.open", "uncertain_action": False, "status": "pending"})
    engine.state = {"entry": "douyin-direct", "index": 6, "pending_request": str(pending),
                    "body_resume": {"attempt": str(attempt), "last_observation": str(attempt / "002-scroll")},
                    "products_resume": checkpoint}
    capture = AnswerCapture(phone, engine)
    capture.attempt, capture.counter, capture.package = attempt, 2, phone.package
    capture.viewport, capture.geometry = viewport, (first.logical_size, first.physical_size)
    capture._settled = lambda initial=None: phone.observe(ocr=True)
    return capture, phone, engine, body, checkpoint, last, pending


def test_entry_restore_preserves_body_and_only_scrolls_to_a_real_saved_entry(recovery):
    capture, phone, engine, body, checkpoint, last, pending = recovery
    originals = [open(path, "rb").read() for path in body["frame_files"]]
    original_body = deepcopy(body)
    current = capture._restore_products_entry(last, body, checkpoint)
    assert _visible_products_entry(current, capture.viewport)["text"] == "全部药品>"
    assert len(phone.swipes) == 1 and checkpoint["phase"] == "open"
    assert checkpoint["entry_relocation"]["complete"]
    assert checkpoint["entry_relocation"]["trace"][-1]["seam"]["verified"]
    assert body == original_body
    assert [open(path, "rb").read() for path in body["frame_files"]] == originals
    assert read_json(pending)["status"] == "superseded_by_verified_entry_relocation"
    assert any(event == "products_entry_relocated" for event, _ in engine.events)


@pytest.mark.parametrize("uncertain", ["inflight", "exploration", "request"])
def test_entry_restore_never_moves_an_uncertain_toggle_scene(recovery, uncertain):
    capture, phone, engine, body, checkpoint, last, pending = recovery
    if uncertain == "inflight":
        engine.state["inflight"] = {"step": "products.open", "risk": "toggle"}
    elif uncertain == "exploration":
        engine.state["exploration"] = {"dispatched": True}
    else:
        write_json(pending, {"step": "products.open", "uncertain_action": True})
    assert capture._restore_products_entry(last, body, checkpoint) is last
    assert not phone.swipes and "entry_relocation" not in checkpoint


def test_changed_body_scene_cannot_be_used_to_find_an_old_products_entry(recovery):
    capture, phone, _, body, checkpoint, last, _ = recovery
    phone.offset = 1030
    with pytest.raises(CaptureError, match="已采正文末帧不一致"):
        capture._restore_products_entry(phone.observe(), body, checkpoint)
    assert not phone.swipes


def test_entry_restore_requires_original_observation_to_reproduce_saved_frame(recovery):
    capture, phone, _, body, checkpoint, last, _ = recovery
    with Image.open(body["frame_files"][0]) as original:
        Image.new("RGB", original.size, "white").save(body["frame_files"][0])
    assert capture._restore_products_entry(last, body, checkpoint) is last
    assert not phone.swipes and "entry_relocation" not in checkpoint


def test_interrupted_relocation_checks_dispatched_scroll_before_swiping_again(recovery):
    capture, phone, _, body, checkpoint, last, pending = recovery
    def interrupted(initial=None):
        raise OSError("connection lost after scroll")
    capture._settled = interrupted
    with pytest.raises(OSError):
        capture._restore_products_entry(last, body, checkpoint)
    assert checkpoint["entry_relocation"]["swipe_pending"] and len(phone.swipes) == 1
    capture._settled = lambda initial=None: phone.observe()
    current = capture._restore_products_entry(phone.observe(), body, checkpoint)
    assert _visible_products_entry(current, capture.viewport)
    assert len(phone.swipes) == 1 and checkpoint["entry_relocation"]["complete"]
    assert read_json(pending)["status"] == "superseded_by_verified_entry_relocation"
