from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from geo_collector.capture import AnswerCapture, answer_page_ready, answer_viewport, capture_viewport, citation_state, drawer_state, guide_visible, network_error_visible, product_state, question_bubble
from geo_collector.device import Observation
from geo_collector.images import CaptureError, image_size
from geo_collector.rules import locate
from geo_collector.storage import write_json


PACKAGE = "example.doctor"


class FakeDevice:
    def __init__(self, scale=1, logical_scale=1, *, references=True, products=False):
        self.scale, self.logical_scale = scale, logical_scale
        self.package = PACKAGE
        self.offset, self.expanded = 0, False
        self.references, self.products = references, products
        self.question = "Example full question"
        self.swipes = []
        self.height = 1700

    def observe(self, ocr=False):
        document = Image.new("RGB", (360, self.height), "white")
        draw = ImageDraw.Draw(document)
        draw.rounded_rectangle((90, 30, 340, 110), 22, fill="#00bdb2")
        draw.text((120, 56), "Example full question", fill="white")
        texts = [{"text": self.question, "confidence": .99, "bounds": (115, 52, 320, 76)}]
        if self.references:
            title = "参考1篇药品说明书"
            draw.rectangle((15, 140, 345, 180 if not self.expanded else 216), fill="#f7f7f7")
            draw.text((24, 150), "REFERENCE", fill="black")
            texts.append({"text": title, "confidence": .99, "bounds": (24, 148, 250, 169)})
            if self.expanded:
                draw.text((24, 185), "1. Example medicine instructions", fill="#666666")
                texts.append({"text": "1.示例药品说明书", "confidence": .99, "bounds": (24, 181, 280, 202)})
        rng = np.random.default_rng(32)
        for y in range(260, self.height - 90, 29):
            text = " ".join(str(n) for n in rng.integers(100, 999, 14))
            draw.text((16, y), text, fill="black")
            texts.append({"text": "Clinical answer " + text, "confidence": .99, "bounds": (16, y, 344, y + 14)})
        draw.text((15, self.height - 50), "END OF ANSWER", fill="#444444")
        if self.products == "inline":
            draw.rectangle((15, 750, 345, 905), fill="#f7f7f7")
            draw.text((25, 765), "Reference product / Example medicine", fill="black")
            document.paste(Image.fromarray(np.random.default_rng(9).integers(0, 255, size=(60, 60, 3), dtype=np.uint8)), (25, 795))
            draw.text((150, 865), "BUY", fill="black")
            texts += [{"text": text, "confidence": .99, "bounds": box} for text, box in [
                ("参考药品", (25, 760, 150, 780)), ("示例药品", (115, 805, 230, 828)), ("去购买", (150, 860, 235, 885))]]
        elif self.products:
            texts.append({"text": "查看全部药品", "confidence": .99, "bounds": (150, self.height - 100, 320, self.height - 80)})
        screen = Image.new("RGB", (360, 800), "#eeeeee")
        screen.paste(document.crop((0, round(self.offset), 360, round(self.offset) + 600)), (0, 80))
        # Do not invent blended pixels between the scrolled body and fixed chrome.
        scaled = screen.resize((round(360 * self.scale), round(800 * self.scale)), Image.Resampling.NEAREST)
        output = BytesIO()
        scaled.save(output, format="PNG")
        visible = []
        for line in texts:
            left, top, right, bottom = line["bounds"]
            top, bottom = top - self.offset + 80, bottom - self.offset + 80
            if 80 <= top < bottom <= 680:
                visible.append({**line, "bounds": tuple(round(value * self.scale) for value in (left, top, right, bottom))})
        nodes = [{"text": "", "class_name": "android.widget.ScrollView", "scrollable": True, "bounds": tuple(round(value * self.logical_scale) for value in (0, 80, 360, 680)), "package": self.package},
                 {"text": "输入问题", "class_name": "android.widget.EditText", "bounds": tuple(round(value * self.logical_scale) for value in (0, 700, 360, 775)), "package": self.package}]
        if self.products == "inline" and 80 <= 875 - self.offset < 935 - self.offset <= 680:
            nodes.append({"text": "", "class_name": "android.view.View", "scrollable": False, "package": PACKAGE,
                          "bounds": tuple(round(value * self.logical_scale) for value in (25, 875 - self.offset, 85, 935 - self.offset))})
        return Observation(output.getvalue(), "<hierarchy />", self.package, "DoctorActivity", (round(360 * self.logical_scale), round(800 * self.logical_scale)), scaled.size, "1", nodes, visible)

    def recognize(self, observation):
        return observation.texts

    def screenshot(self):
        return self.observe().png

    def swipe(self, start, end, duration=.4, expected_package=None):
        assert expected_package == self.package
        assert all(0 <= value <= limit for point in (start, end) for value, limit in zip(point, (360 * self.logical_scale, 800 * self.logical_scale)))
        self.swipes.append((start, end))
        self.offset = max(0, min(self.height - 600, self.offset + (start[1] - end[1]) / self.logical_scale))


class FakeEngine:
    def __init__(self, device):
        self.device = device
        self.calls, self.events = [], []
        self.state = {}

    def log(self, event, **details):
        self.events.append((event, details))

    def save(self):
        pass

    def layout(self, role, observation, check, **kwargs):
        return check(observation)

    def commit_layouts(self):
        pass

    def resolve_capture_request(self, step):
        pass

    def _request(self, step, goal, observation, hints, reason, **kwargs):
        raise CaptureError(reason)

    def perform(self, step, goal, package, postcondition, **options):
        self.calls.append((step, options))
        before = self.device.observe(ocr=True)
        if postcondition(before):
            return before
        if step == "citation.expand":
            assert options["risk"] == "toggle"
            self.device.expanded = True
        elif step == "overlay.dismiss":
            assert options["risk"] == "toggle" and options["defaults"] == ()
            self.device.guide_active = False
        elif step == "products.open":
            self.device.drawer = True
            self.device.opens += 1
            if self.device.interrupt_open:
                self.device.interrupt_open = False
                raise RuntimeError("simulated interruption after opening")
        elif step == "capture.close_existing_products":
            assert options["risk"] == "toggle" and options["defaults"] == ()
            self.device.drawer = False
        elif step == "products.expand":
            action = options["defaults"][0]["actions"][0]
            assert action["type"] == "swipe" and action["direction"] == "down"
            start = locate(action["target"], before, Path("/unused"))
            self.device.swipe(start, (start[0], before.logical_size[1] * .035), expected_package=package)
        else:
            raise AssertionError(step)
        result = self.device.observe(ocr=True)
        assert postcondition(result)
        return result


def fast_capture(device):
    engine = FakeEngine(device)
    capture = AnswerCapture(device, engine)
    clock = [0.0]
    capture._clock = lambda: clock[0]
    capture._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    return capture, engine


@pytest.mark.parametrize("source_question", ["Example full question", "Wrong question", None])
def test_search_answer_without_question_requires_matching_entry_and_complete_pixels(tmp_path, source_question):
    from geo_collector.capture import verify_capture_images
    from geo_collector.platforms import toutiao
    from geo_collector.recovery import save_observation
    from test_toutiao import ToutiaoPhone

    class AnswerOnly(FakeDevice):
        def observe(self, ocr=False):
            item = super().observe(ocr)
            item.texts = [line for line in item.texts if line["text"] != self.question]
            picture = Image.open(BytesIO(item.png))
            if self.offset < 110:
                ImageDraw.Draw(picture).rectangle((90, max(80, 110 - self.offset), 340, 190 - self.offset), fill="white")
            output = BytesIO()
            picture.save(output, "PNG")
            item.png = output.getvalue()
            return item

    device = AnswerOnly(references=False)
    device.package = toutiao.PACKAGE
    capture, engine = fast_capture(device)
    capture.answer_page_check = lambda o: o.package == toutiao.PACKAGE
    engine.state["entry"] = "toutiao"
    debug, delivery = tmp_path / "debug", tmp_path / "delivery"
    if source_question:
        search = ToutiaoPhone()
        search.stage, search.question = "results", source_question
        save_observation(debug / "entry-source", search.observe())
    if source_question != device.question:
        with pytest.raises(CaptureError, match="完整问题气泡"):
            capture.run(device.question, device.package, delivery, debug)
        assert not delivery.exists()
        return
    result = capture.run(device.question, device.package, delivery, debug)
    assert result["question_visible"] is False
    assert result["citations_found"] is False and result["products"]["present"] is False
    assert result["end_confirmed"] and result["frame_count"] > 1
    verify_capture_images(result, debug, delivery)
    (delivery / "搜索结果.png").write_bytes(device.observe().png)
    with pytest.raises(CaptureError, match="原始观察不符"):
        verify_capture_images(result, debug, delivery)


@pytest.mark.parametrize("drawer", [False, True])
def test_delivery_validation_precedes_checkpoint_cleanup_and_rule_promotion(tmp_path, monkeypatch, drawer):
    device = DrawerDevice() if drawer else FakeDevice(references=False)
    capture, engine = fast_capture(device)
    delivery = tmp_path / "delivery"
    original_write = Path.write_bytes

    def replace_delivery(path, data):
        if path.parent == delivery:
            buffer = BytesIO()
            Image.new("RGB", (1, 1)).save(buffer, format="PNG")
            data = buffer.getvalue()
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", replace_delivery)
    monkeypatch.setattr(engine, "commit_layouts", lambda: pytest.fail("unverified delivery must not promote rules"))
    with pytest.raises(CaptureError, match="正式图片像素"):
        capture.run(device.question, PACKAGE, delivery, tmp_path / "debug")
    checkpoint = engine.state["products_resume" if drawer else "body_resume"]
    assert checkpoint["frame_files"]
    if drawer:
        assert checkpoint["phase"] == "list"


def test_generation_may_change_longer_than_scroll_settle_timeout(tmp_path):
    from dataclasses import replace

    class StreamingDevice(FakeDevice):
        def observe(self, ocr=False):
            item = super().observe(ocr)
            if elapsed[0] < 30:
                elapsed[0] += 5
                picture = Image.open(BytesIO(item.png)).convert("RGB")
                ImageDraw.Draw(picture).rectangle((0, 150, 360, 650), fill="black" if round(elapsed[0] / 5) % 2 else "white")
                data = BytesIO()
                picture.save(data, format="PNG")
                item = replace(item, png=data.getvalue())
            return item

    elapsed = [0.0]
    device = StreamingDevice()
    capture, engine = fast_capture(device)
    capture._clock = lambda: elapsed[0]
    capture._sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["completion_confirmed"] and result["end_confirmed"]
    assert all(seam["verified"] for seam in result["seams"])
    assert elapsed[0] >= 30


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_complete_answer_expands_references_once_and_maps_swipes(tmp_path, scale, logical_scale):
    device = FakeDevice(scale, logical_scale)
    capture, engine = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["question_confirmed"] and result["citations_checked"] and result["citations_expanded"]
    assert result["completion_confirmed"] and result["end_confirmed"]
    assert result["frame_count"] > 1 and len(result["seams"]) == result["frame_count"] - 1
    assert all(item["verified"] for item in result["seams"])
    assert len(engine.calls) == 1 and engine.calls[0][1]["risk"] == "toggle"
    assert not result["products"]["present"] and result["products"]["mode"] == "none"
    assert all(str(tmp_path / "delivery") in path for path in result["image_paths"])
    assert {path.suffix for path in (tmp_path / "delivery").iterdir()} == {".png"}
    assert image_size((tmp_path / "delivery" / "回答_001.png").read_bytes())[0] == 360 * scale
    assert any(event == "answer_generation_complete" and details["quiet_seconds"] >= 5 for event, details in engine.events)


@pytest.mark.parametrize("scale", [1, 2])
def test_exact_wrapped_question_requires_real_bubble_and_references_need_rows(scale):
    device = FakeDevice(scale)
    observation = device.observe(ocr=True)
    viewport = answer_viewport(observation)
    line = observation.texts[0]
    observation.texts[:1] = [{"text": "Example full", "confidence": .99, "bounds": tuple(value * scale for value in (115, 125, 315, 145))},
                            {"text": "question", "confidence": .99, "bounds": tuple(value * scale for value in (115, 153, 240, 175))}]
    assert question_bubble(observation, "Example full question", viewport)
    assert question_bubble(observation, "Another question", viewport) is None
    observation.texts[:2] = [{**line, "bounds": tuple(value * scale for value in (10, 310, 300, 335))}]
    assert question_bubble(observation, device.question, viewport) is None
    assert not citation_state(observation, viewport)["expanded"]
    next(line for line in observation.texts if line["text"].startswith("参考"))["text"] += "^"
    assert not citation_state(observation, viewport)["expanded"]


def test_exact_question_keeps_meaningful_punctuation():
    device = FakeDevice()
    device.question = "每天服用1.5mg可以吗"
    current = device.observe(ocr=True)
    viewport = answer_viewport(current)
    assert question_bubble(current, device.question, viewport)
    assert question_bubble(current, "每天服用15mg可以吗", viewport) is None


def test_unknown_citations_and_missing_question_fail_without_delivery(tmp_path):
    device = FakeDevice()
    observation = device.observe(ocr=True)
    next(line for line in observation.texts if line["text"].startswith("参考"))["text"] = "参考1篇新的资料类型"
    with pytest.raises(CaptureError, match="未知引用"):
        citation_state(observation, answer_viewport(observation))
    capture, engine = fast_capture(device)
    with pytest.raises(CaptureError, match="历史会话顶部"):
        capture.run("This question was never sent", PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert not (tmp_path / "delivery").exists()
    assert list((tmp_path / "debug").glob("**/failure.json"))
    assert not engine.calls


def test_inline_products_crossing_viewports_remain_present_after_their_card_scrolls_away(tmp_path):
    device = FakeDevice(products="inline")
    capture, _ = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["products"]["present"] and result["products"]["mode"] == "inline"
    assert all(result["products"][key] for key in ("complete", "first_item_confirmed", "images_ready", "end_confirmed", "seams_verified"))


def test_seam_failure_preserves_debug_and_never_publishes_partial_answer(tmp_path, monkeypatch):
    device = FakeDevice(references=False)
    capture, _ = fast_capture(device)
    calls = []

    def reject(*args, **kwargs):
        calls.append(True)
        raise CaptureError("unverified test seam")

    monkeypatch.setattr("geo_collector.capture.match_overlap", reject)
    with pytest.raises(CaptureError, match="unverified"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert len(calls) == 2  # one local read-only recapture, never a question resend
    assert not (tmp_path / "delivery").exists()
    assert list((tmp_path / "debug").glob("**/*seam-recapture/screen.png"))


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_drawer_expansion_uses_panel_edge_not_title_text(scale, logical_scale):
    from dataclasses import replace
    observation = FakeDevice(scale, logical_scale).observe()
    def node(box, **attrs):
        return {"package": PACKAGE, "bounds": [v * logical_scale for v in box], **attrs}
    observation = replace(observation, texts=[
        {"text": "参考药品", "bounds": [v * scale for v in (146, 171, 213, 192)], "confidence": .99},
        {"text": "首项药品", "bounds": [v * scale for v in (10, 215, 85, 231)], "confidence": .99}], nodes=[
        node((0, 0, 360, 800)), node((0, 157, 360, 786)),
        node((0, 205, 360, 786), scrollable=True)])
    state = drawer_state(observation)
    assert state["expanded"] and state["first_item"] == "首项药品"
    assert state["panel_top"] == 157 * scale
    # A full-screen root alone cannot prove a half-screen drawer expanded.
    without_panel = replace(observation, nodes=[observation.nodes[0], observation.nodes[2]])
    assert not drawer_state(without_panel)["expanded"]


def test_short_drawer_list_is_detected_when_recycler_view_is_not_scrollable():
    from dataclasses import replace

    observation = FakeDevice().observe()
    observation = replace(observation, texts=[
        {"text": "参考药品", "bounds": (146, 330, 213, 351), "confidence": .99},
        {"text": "首项药品", "bounds": (20, 380, 100, 400), "confidence": .99},
    ], nodes=[
        {"package": PACKAGE, "bounds": (0, 314, 360, 786), "class_name": "android.widget.FrameLayout"},
        {"package": PACKAGE, "bounds": (0, 362, 360, 786), "class_name": "androidx.recyclerview.widget.RecyclerView", "scrollable": False},
    ])

    state = drawer_state(observation)
    assert state and state["first_item"] == "首项药品" and not state["expanded"]


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
@pytest.mark.parametrize("variant", ["plain", "wrapped", "truncated"])
def test_drawer_canvas_card_images_require_photo_pixels(scale, logical_scale, variant):
    from dataclasses import replace
    from geo_collector.capture import _product_images_ready
    observation = FakeDevice(scale, logical_scale).observe()
    texts = [{"text": text, "bounds": [v * scale for v in box], "confidence": .99} for text, box in [
        ("首项药品", (0, 180, 330, 198)), ("首项药品", (20, 355, 100, 375)), ("查看说明书", (25, 420, 125, 440))]]
    if variant == "wrapped":
        texts[0]["text"] = "首项药品名称"
        texts.append({"text": "名称", "bounds": [v * scale for v in (20, 380, 50, 397)], "confidence": .99})
    elif variant == "truncated":
        texts[0]["text"] = "Lek Pharmaceuticals d.d. medicine"
        texts[1]["text"] = "Lek"
        texts.append({"text": "Pharmaceutical...", "bounds": [v * scale for v in (20, 380, 130, 397)], "confidence": .99})
    nodes = [{"package": PACKAGE, "class_name": "android.view.ViewGroup", "bounds": [v * logical_scale for v in (10, 210, 150, 470)]}]
    for loaded in (False, True):
        picture = Image.new("RGB", (360, 800), "white")
        if loaded:
            picture.paste(Image.fromarray(np.random.default_rng(32).integers(0, 255, (130, 130, 3), dtype=np.uint8)), (15, 215))
        data = BytesIO()
        picture.resize(observation.physical_size).save(data, format="PNG")
        item = replace(observation, png=data.getvalue(), nodes=nodes, texts=texts)
        ready, boxes = _product_images_ready(item, tuple(v * scale for v in (0, 170, 360, 780)))
        assert ready is loaded and len(boxes) == 1


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_short_native_drawer_finds_photo_inside_full_width_rows(scale, logical_scale):
    from dataclasses import replace
    from geo_collector.capture import _product_images_ready

    observation = FakeDevice(scale, logical_scale).observe()
    nodes = [
        {"package": PACKAGE, "class_name": "androidx.recyclerview.widget.RecyclerView", "scrollable": False,
         "bounds": [v * logical_scale for v in (0, 120, 360, 760)]},
        {"package": PACKAGE, "class_name": "android.widget.FrameLayout",
         "bounds": [v * logical_scale for v in (18, 120, 342, 190)]},
        {"package": PACKAGE, "class_name": "android.widget.FrameLayout",
         "bounds": [v * logical_scale for v in (18, 140, 342, 260)]},
    ]
    for loaded in (False, True):
        picture = Image.new("RGB", observation.physical_size, "white")
        if loaded:
            picture.paste(Image.fromarray(np.random.default_rng(41).integers(0, 255, (60 * scale, 60 * scale, 3), dtype=np.uint8)),
                          (28 * scale, 170 * scale))
        data = BytesIO()
        picture.save(data, format="PNG")
        item = replace(observation, png=data.getvalue(), nodes=nodes, texts=[])
        ready, boxes = _product_images_ready(item, tuple(v * scale for v in (0, 120, 360, 760)))
        assert ready is loaded and len(boxes) == 1


def test_network_error_requires_retry_and_real_answer_viewport():
    from dataclasses import replace

    recovered = FakeDevice().observe(ocr=True)
    failed = replace(recovered, texts=[
        {"text": "网络不稳定，请重试", "bounds": (120, 350, 250, 380), "confidence": .99},
        {"text": "重试", "bounds": (145, 430, 215, 460), "confidence": .99},
    ], nodes=[])
    class Engine:
        def __init__(self):
            self.calls = []
        def perform(self, step, goal, package, postcondition, **options):
            self.calls.append((step, options))
            loading = replace(recovered, texts=[{"text":"加载中", "bounds":(120,350,250,380), "confidence":.99}])
            assert not postcondition(loading)  # An empty viewport after retry is not a recovered answer.
            assert postcondition(recovered)
            return recovered
        def log(self, *args, **kwargs):
            pass
    capture = AnswerCapture(FakeDevice(), Engine())
    capture.package, capture.attempt = PACKAGE, Path("/tmp")
    capture._save = lambda *args: None
    assert network_error_visible(failed)
    assert capture._recover_network(failed) is recovered
    assert capture.engine.calls[0][0] == "network.retry"


@pytest.mark.parametrize("title", ["参考药品", "参考药品（2种）", "推荐药品 (2款)"])
@pytest.mark.parametrize("title_source", ["ocr", "node"])
def test_inline_purchase_card_is_not_a_drawer_and_unknown_viewport_is_rejected(title, title_source):
    device = FakeDevice()
    observation = device.observe(ocr=True)
    observation.texts = [{"text": title, "confidence": .99, "bounds": (20, 330, 160, 355)},
                         {"text": "示例药品", "confidence": .99, "bounds": (20, 380, 160, 405)},
                         {"text": "去购买", "confidence": .99, "bounds": (150, 420, 220, 448)}]
    if title_source == "node":
        heading = observation.texts.pop(0)
        observation.nodes.append({"text": heading["text"], "bounds": heading["bounds"], "package": PACKAGE})
    observation.nodes.append({"text": "", "class_name": "android.widget.ImageView", "scrollable": False, "package": PACKAGE, "bounds": (230, 365, 280, 415)})
    state = product_state(observation, answer_viewport(observation))
    assert state["present"] and state["mode"] == "inline" and state["complete"]
    observation.nodes.pop()
    assert not product_state(observation, answer_viewport(observation))["images_ready"]
    observation.nodes.append({"text": "示例药品", "class_name": "android.view.ViewGroup", "scrollable": False, "package": PACKAGE, "bounds": (20, 380, 335, 405)})
    observation.texts.append({"text": "示例药品适应症描述", "confidence": .99, "bounds": (20, 406, 330, 418)})
    text_only = product_state(observation, answer_viewport(observation))
    assert text_only["complete"] and text_only["image_requirement"] == "none" and text_only["text_only"]
    observation.nodes = []
    with pytest.raises(CaptureError, match="视口"):
        answer_viewport(observation)


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_answer_viewport_excludes_actual_shortcut_container_above_input(scale, logical_scale):
    observation = FakeDevice(scale, logical_scale).observe(ocr=True)
    observation.nodes[0]["bounds"] = tuple(round(value * logical_scale) for value in (0, 80, 360, 720))
    observation.nodes.append({"text": "", "class_name": "android.widget.HorizontalScrollView", "scrollable": True,
                              "bounds": tuple(round(value * logical_scale) for value in (0, 650, 360, 692)), "package": PACKAGE})
    observation.nodes.append({"text": "", "class_name": "android.view.ViewGroup", "scrollable": False,
                              "bounds": tuple(round(value * logical_scale) for value in (0, 110, 360, 650)), "package": PACKAGE})
    observation.texts += [{"text": text, "confidence": .99, "bounds": tuple(round(value * scale) for value in box)}
                          for text, box in [("报告解读", (20, 664, 90, 684)), ("立即下载", (135, 664, 220, 684))]]
    assert answer_viewport(observation)[3] == 650 * scale
    assert answer_viewport(observation)[1] == 110 * scale
    observation.texts.pop()  # One phrase in answer text does not prove a toolbar.
    assert answer_viewport(observation)[3] > 650 * scale


def test_ocr_composer_requires_a_wide_bottom_container():
    observation = FakeDevice().observe(ocr=True)
    observation.nodes = observation.nodes[:1]
    observation.texts = [{"text": "按住说话", "confidence": .99, "bounds": (140, 720, 220, 750)}]
    assert not answer_page_ready(observation)
    observation.nodes.append({"text": "", "class_name": "android.view.ViewGroup", "package": PACKAGE,
                              "bounds": (50, 700, 330, 775)})
    assert answer_page_ready(observation)
    assert answer_viewport(observation)[3] < 700


def test_answer_text_starting_with_input_hint_is_not_a_crop_boundary():
    observation = FakeDevice().observe(ocr=True)
    expected = answer_viewport(observation)
    observation.texts.append({"text": "输入问题时需要说明症状持续时间", "confidence": .99,
                              "bounds": (20, 610, 340, 635)})
    assert answer_viewport(observation) == expected


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
@pytest.mark.parametrize("notice_source", ["node", "ocr"])
def test_toutiao_legacy_notice_requires_top_webview_and_answer_shell(scale, logical_scale, notice_source):
    from dataclasses import replace

    package = "com.ss.android.article.news"
    observation = FakeDevice(scale, logical_scale).observe(ocr=True)
    observation.package = package
    for node in observation.nodes:
        node["package"] = package
    def node(text, box, **attrs):
        return {"text": text, "package": package, "bounds": tuple(round(value * logical_scale) for value in box), **attrs}
    title = node("小荷AI医生", (100, 30, 260, 60), resource_id=package + ":id/title", class_name="android.widget.TextView")
    web = node("小荷AI医生", (0, 80, 360, 780), class_name="android.webkit.WebView")
    observation.nodes += [title, web]
    notice = {"text": "AI生成非医疗诊断，仅供参考", "confidence": .99,
              "bounds": tuple(round(value * (logical_scale if notice_source == "node" else scale)) for value in (18, 90, 342, 110))}
    if notice_source == "node":
        notice.update(package=package, class_name="android.widget.TextView")
        observation.nodes.append(notice)
    else:
        observation.texts.append(notice)
    assert 110 * scale < answer_viewport(observation)[1] <= 116 * scale
    for missing in (title, web, observation.nodes[1]):
        incomplete = replace(observation, nodes=[item for item in observation.nodes if item is not missing])
        assert answer_viewport(incomplete)[1] == 80 * scale
    other_app = replace(observation, package=PACKAGE, nodes=[{**item, "package": PACKAGE} for item in observation.nodes])
    assert answer_viewport(other_app)[1] == 80 * scale
    # Even a matching phrase near the top of real answer content is not fixed chrome.
    notice["bounds"] = tuple(round(value * (logical_scale if notice_source == "node" else scale)) for value in (18, 135, 342, 157))
    assert answer_viewport(observation)[1] == 80 * scale


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_patient_strip_defines_top_even_before_answer_becomes_scrollable(scale, logical_scale):
    observation = FakeDevice(scale, logical_scale).observe(ocr=True)
    observation.nodes[0].update(scrollable=False, bounds=tuple(round(v * logical_scale) for v in (0, 110, 360, 680)))
    observation.nodes[1]["bounds"] = tuple(round(v * logical_scale) for v in (0, 690, 360, 775))
    for label, box in [("", (0, 64, 360, 680)), ("", (0, 64, 360, 104)), ("不选咨询人", (20, 75, 220, 96))]:
        observation.nodes.append({"text": label, "class_name": "android.view.View", "scrollable": False, "package": PACKAGE,
                                  "bounds": tuple(round(v * logical_scale) for v in box)})
    assert answer_viewport(observation)[1] == 110 * scale


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_generation_refreshes_initial_non_scrollable_viewport_before_formal_capture(tmp_path, scale, logical_scale):
    device = FakeDevice(scale, logical_scale, references=False)
    observe = device.observe
    calls = [0]
    def initially_short(ocr=False):
        observation = observe(ocr)
        calls[0] += 1
        if calls[0] == 1:
            observation.nodes[0]["scrollable"] = False
        return observation
    device.observe = initially_short
    capture, engine = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["geometry"]["viewport_physical"] == [0, 80 * scale, 360 * scale, 680 * scale]
    assert any(event == "answer_viewport_refreshed" for event, _ in engine.events)


class DrawerDevice(FakeDevice):
    def __init__(self, scale=1, logical_scale=1, *, product_height=1450, unloaded=False, interrupt_open=False):
        super().__init__(scale, logical_scale, references=False, products=True)
        self.drawer = self.drawer_expanded = False
        self.product_offset, self.product_height = 0, product_height
        self.unloaded, self.interrupt_open = unloaded, interrupt_open
        self.opens = 0
        self.drawer_swipes = []
        self.body_after_drawer = 0

    def observe(self, ocr=False):
        if not self.drawer:
            return super().observe(ocr)
        title_top, viewport_top = (65, 110) if self.drawer_expanded else (350, 400)
        viewport_bottom = 760
        document = Image.new("RGB", (360, self.product_height), "white")
        draw = ImageDraw.Draw(document)
        texts, images = [], []
        rng = np.random.default_rng(117)
        for index, top in enumerate(range(20, self.product_height - 140, 235), 1):
            ib = (18, top + 12, 96, top + 90)
            if self.unloaded:
                draw.rectangle(ib, fill="#eeeeee")
            else:
                pixels = rng.integers(15, 240, size=(78, 78, 3), dtype=np.uint8)
                document.paste(Image.fromarray(pixels), (ib[0], ib[1]))
            images.append(ib)
            name = f"Medicine number {index}"
            draw.text((115, top + 15), name, fill="black")
            texts.append({"text": name, "confidence": .99, "bounds": (115, top + 12, 335, top + 31)})
            for yy in range(top + 50, top + 195, 24):
                draw.text((112, yy), " ".join(str(x) for x in rng.integers(1, 999, 9)), fill="#333333")
        screen = Image.new("RGB", (360, 800), "#dddddd")
        draw = ImageDraw.Draw(screen)
        draw.rectangle((0, title_top - 15, 360, viewport_bottom), fill="white")
        draw.text((110, title_top), "REFERENCE PRODUCTS", fill="black")
        screen.paste(document.crop((0, round(self.product_offset), 360, round(self.product_offset) + viewport_bottom - viewport_top)), (0, viewport_top))
        screen = screen.resize((round(360 * self.scale), round(800 * self.scale)), Image.Resampling.NEAREST)
        output = BytesIO(); screen.save(output, format="PNG")
        visible = [{"text": "参考药品", "confidence": .99, "bounds": tuple(round(value * self.scale) for value in (110, title_top, 260, title_top + 22))}]
        nodes = [{"text": "", "class_name": "android.widget.ScrollView", "scrollable": True, "package": PACKAGE,
                  "bounds": tuple(round(value * self.logical_scale) for value in (0, viewport_top, 360, viewport_bottom))}]
        for line in texts:
            left, top, right, bottom = line["bounds"]
            top, bottom = top - self.product_offset + viewport_top, bottom - self.product_offset + viewport_top
            if viewport_top <= top < bottom <= viewport_bottom:
                visible.append({**line, "bounds": tuple(round(value * self.scale) for value in (left, top, right, bottom))})
        for left, top, right, bottom in images:
            top, bottom = top - self.product_offset + viewport_top, bottom - self.product_offset + viewport_top
            if viewport_top <= top < bottom <= viewport_bottom:
                nodes.append({"text": "", "class_name": "android.widget.ImageView", "scrollable": False, "package": PACKAGE,
                              "bounds": tuple(round(value * self.logical_scale) for value in (left, top, right, bottom))})
        return Observation(output.getvalue(), "<hierarchy />", PACKAGE, "DoctorActivity", (round(360 * self.logical_scale), round(800 * self.logical_scale)), screen.size, "1", nodes, visible)

    def swipe(self, start, end, duration=.4, expected_package=None):
        if not self.drawer:
            return super().swipe(start, end, duration, expected_package)
        assert expected_package == PACKAGE
        self.drawer_swipes.append((start, end, self.drawer_expanded))
        assert end[1] < start[1]  # Never reverse the list to recover its first item.
        if not self.drawer_expanded:
            assert 350 <= start[1] / self.logical_scale <= 372  # From the title, not a medicine card.
            self.drawer_expanded = True
        else:
            self.product_offset = max(0, min(max(0, self.product_height - 650), self.product_offset + (start[1] - end[1]) / self.logical_scale))


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_drawer_expands_by_header_before_first_frame_and_captures_real_end(tmp_path, scale, logical_scale):
    device = DrawerDevice(scale, logical_scale)
    capture, engine = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    products = result["products"]
    assert all(products[key] for key in ("present", "complete", "first_item_confirmed", "images_ready", "end_confirmed", "seams_verified", "terminal_sequence"))
    assert products["mode"] == "drawer" and products["frame_count"] > 1
    assert products["end_probes"] >= 2 and all(seam["verified"] for seam in products["seams"])
    assert not products["restore_required"] and products["post_scan_swipes"] == 0
    assert device.product_offset == device.product_height - 650
    assert device.opens == 1 and not device.drawer_swipes[0][2]
    assert "products_resume" not in engine.state
    assert len(result["image_paths"]) == 2 and products["image_paths"][0].endswith("回答_参考药品_001.png")
    first = Image.open(products["frame_files"][0])
    assert first.height == 695 * scale  # Fullscreen title/list; no half-screen first fragment.
    assert {path.suffix for path in (tmp_path / "delivery").iterdir()} == {".png"}


def test_drawer_entry_does_not_end_body_capture_before_scroll_end(tmp_path):
    device = DrawerDevice(product_height=400)
    capture, _ = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["end_confirmed"]
    assert device.offset == device.height - 600


def test_short_drawer_uses_multiple_touchpoints_and_three_end_probes(tmp_path):
    device = DrawerDevice(product_height=400)
    capture, _ = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["products"]["frame_count"] == 1 and result["products"]["end_probes"] == 3
    assert len({round(start[0]) for start, _, expanded in device.drawer_swipes if expanded}) >= 2


def test_unloaded_drawer_images_stop_without_scrolling_products_or_publishing(tmp_path):
    device = DrawerDevice(unloaded=True)
    capture, engine = fast_capture(device)
    with pytest.raises(CaptureError, match="图片未加载"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert len(device.drawer_swipes) == 1  # Header expansion only.
    assert engine.state["products_resume"]["phase"] == "expand"
    assert not (tmp_path / "delivery").exists()


def test_drawer_open_interruption_resumes_terminal_phase_without_reopening_or_body_scroll(tmp_path):
    device = DrawerDevice(product_height=400, interrupt_open=True)
    capture, engine = fast_capture(device)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    body_swipes = len(device.swipes)
    assert engine.state["products_resume"]["phase"] == "open"
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["products"]["complete"] and device.opens == 1
    assert len(device.swipes) == body_swipes
    assert any(event == "products_terminal_resumed" for event, _ in engine.events)


def test_silent_empty_answer_and_unknown_based_on_citation_cannot_pass(tmp_path):
    device = FakeDevice(references=False)
    observe = device.observe
    def only_question(ocr=False):
        observation = observe(ocr)
        observation.texts = [line for line in observation.texts if line["text"] == device.question]
        return observation
    device.observe = only_question
    capture, _ = fast_capture(device)
    capture.generation_timeout_seconds = 8
    with pytest.raises(CaptureError, match="未确认实际回答正文"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert not (tmp_path / "delivery").exists()
    observation = observe(ocr=True)
    observation.texts.append({"text": "根据3篇资料为你总结", "confidence": .99, "bounds": (20, 210, 290, 235)})
    with pytest.raises(CaptureError, match="未知引用"):
        citation_state(observation, answer_viewport(observation))


def test_older_text_above_current_question_is_not_proof_of_a_new_answer():
    from geo_collector.capture import _answer_content_visible
    device = FakeDevice(references=False)
    observation = device.observe(ocr=True)
    observation.texts = [observation.texts[0], {"text": "Previous answer from an older question", "confidence": .99, "bounds": (12, 82, 345, 104)}]
    assert not _answer_content_visible(observation, answer_viewport(observation), device.question)


def test_pending_citation_repair_is_consumed_before_any_scroll(tmp_path):
    device = FakeDevice()
    capture, engine = fast_capture(device)
    request = tmp_path / "request.json"
    write_json(request, {"step": "citation.expand", "package": PACKAGE, "entry": "xiaohe", "question_index": 1})
    engine.state.update(entry="xiaohe", index=1, pending_request=str(request))
    perform = engine.perform
    def verify_original_page(*args, **kwargs):
        assert not device.swipes
        result = perform(*args, **kwargs)
        engine.state.pop("pending_request", None)
        return result
    engine.perform = verify_original_page
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["citations_expanded"] and len(engine.calls) == 1


def test_drawer_seam_failure_resumes_last_verified_frame_without_body_rescan(tmp_path, monkeypatch):
    import geo_collector.capture as module
    device = DrawerDevice()
    capture, engine = fast_capture(device)
    original = module.match_product_overlap
    def fail_product_seam(*args, **kwargs):
        if device.drawer and device.product_offset > 0:
            raise CaptureError("simulated product seam failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "match_product_overlap", fail_product_seam)
    with pytest.raises(CaptureError, match="product seam failure"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    body_swipes = len(device.swipes)
    assert engine.state["products_resume"]["phase"] == "list"
    assert not (tmp_path / "delivery").exists()
    monkeypatch.setattr(module, "match_product_overlap", original)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["products"]["complete"] and device.opens == 1 and len(device.swipes) == body_swipes


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_each_frame_excludes_floating_button_and_last_frame_recovers_full_bottom(tmp_path, scale, logical_scale):
    device = FakeDevice(scale, logical_scale, references=False)
    device.package = "com.aurora.xiaohe.aidoctor"
    observe = device.observe
    swipe = device.swipe
    device.bottom_noop = False
    def remember_bottom_probe(*args, **kwargs):
        before = device.offset
        swipe(*args, **kwargs)
        device.bottom_noop = before == device.offset == device.height - 600
    device.swipe = remember_bottom_probe
    def with_overlay(ocr=False):
        observation = observe(ocr)
        if device.offset < device.height - 600 or not device.bottom_noop:
            box = (158, 606, 202, 650)
            pixels = Image.open(BytesIO(observation.png)).convert("RGB")
            ImageDraw.Draw(pixels).ellipse(tuple(round(value * scale) for value in box), fill="#ff0000")
            output = BytesIO(); pixels.save(output, format="PNG"); observation.png = output.getvalue()
            observation.nodes.append({"text": "", "description": "", "resource_id": "", "class_name": "android.view.View", "clickable": True,
                                      "scrollable": False, "package": device.package, "bounds": tuple(round(value * logical_scale) for value in box)})
        return observation
    device.observe = with_overlay
    capture, _ = fast_capture(device)
    result = capture.run(device.question, device.package, tmp_path / "delivery", tmp_path / "debug")
    assert result["occlusion_checked"]
    assert any(seam["shift"] == 0 for seam in result["seams"])  # Extra bottom rows after the overlay alone disappears.
    assert any(info["excluded_controls"] for info in result["frame_viewports"])
    assert not result["frame_viewports"][-1]["excluded_controls"]
    assert result["frame_viewports"][-1]["viewport_physical"][3] == 680 * scale
    image = np.asarray(Image.open(result["image_paths"][0]).convert("RGB"))
    assert not np.any(np.all(image == (255, 0, 0), axis=2))
    final = np.asarray(Image.open(BytesIO(observe().png)).convert("RGB"))
    assert np.array_equal(image[-70 * scale:], final[610 * scale:680 * scale])


class GuideDevice(FakeDevice):
    def __init__(self, scale=1, logical_scale=1, *, during_scroll=False):
        super().__init__(scale, logical_scale, references=False)
        self.guide_active = not during_scroll
        self.during_scroll, self.guide_shown = during_scroll, False

    def observe(self, ocr=False):
        observation = super().observe(ocr)
        if self.guide_active:
            guide = "在这里查看「历史对话」"
            observation.nodes = [{"text": guide, "class_name": "android.widget.TextView", "package": self.package,
                                  "scrollable": False, "bounds": tuple(round(v * self.logical_scale) for v in (20, 74, 280, 98))}]
            observation.texts += [{"text": guide, "confidence": .99, "bounds": tuple(round(v * self.scale) for v in (20, 74, 280, 98))},
                                  {"text": "输入问题", "confidence": .99, "bounds": tuple(round(v * self.scale) for v in (20, 720, 250, 750))}]
        return observation

    def swipe(self, start, end, duration=.4, expected_package=None):
        assert not self.guide_active  # A popup tree must never drive scrolling.
        super().swipe(start, end, duration, expected_package)
        if self.during_scroll and not self.guide_shown and end[1] > start[1]:
            self.guide_active = self.guide_shown = True


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
@pytest.mark.parametrize("during_scroll", [False, True])
def test_guide_overlay_is_repaired_before_layout_or_scroll_and_capture_continues(tmp_path, scale, logical_scale, during_scroll):
    device = GuideDevice(scale, logical_scale, during_scroll=during_scroll)
    capture, engine = fast_capture(device)
    if not during_scroll:
        observation = device.observe(ocr=True)
        assert guide_visible(observation)
        with pytest.raises(CaptureError, match="引导浮层"):
            answer_viewport(observation)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["end_confirmed"] and result["geometry"]["viewport_physical"] == [0, 80 * scale, 360 * scale, 680 * scale]
    assert [step for step, _ in engine.calls] == ["overlay.dismiss"]
    assert list((tmp_path / "debug").glob("**/*guide-overlay/observation.json"))
    assert any(event == "guide_overlay_dismissed" and details["underlying_layout_verified"] for event, details in engine.events)


def test_pending_guide_that_disappeared_is_cleared_before_scrolling(tmp_path):
    device = GuideDevice()
    device.guide_active = False
    capture, engine = fast_capture(device)
    request = tmp_path / "request.json"
    write_json(request, {"step": "overlay.dismiss", "package": PACKAGE, "entry": "xiaohe", "question_index": 5})
    engine.state.update(entry="xiaohe", index=5, pending_request=str(request))
    perform = engine.perform
    def verify_no_scroll(*args, **kwargs):
        assert not device.swipes
        result = perform(*args, **kwargs)
        engine.state.pop("pending_request", None)
        return result
    engine.perform = verify_no_scroll
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["end_confirmed"] and [step for step, _ in engine.calls] == ["overlay.dismiss"]


def test_guide_disappearance_alone_does_not_verify_missing_underlying_hierarchy(tmp_path):
    device = GuideDevice()
    capture, engine = fast_capture(device)
    def missing_layout(step, goal, package, postcondition, **options):
        observation = device.observe(ocr=True)
        observation.nodes = []
        observation.texts = [line for line in observation.texts if "历史对话" not in line["text"]]
        assert not guide_visible(observation)
        assert not postcondition(observation)
        raise CaptureError("底层层级未恢复")
    engine.perform = missing_layout
    with pytest.raises(CaptureError, match="底层层级未恢复"):
        capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert not device.swipes and not (tmp_path / "delivery").exists()


def test_transient_guide_disappears_during_bounded_read_only_wait_without_agent(tmp_path):
    device = GuideDevice()
    observe = device.observe
    reads = [0]
    def transient(ocr=False):
        if device.guide_active:
            reads[0] += 1
            if reads[0] == 3:
                device.guide_active = False
        return observe(ocr)
    device.observe = transient
    capture, engine = fast_capture(device)
    result = capture.run(device.question, PACKAGE, tmp_path / "delivery", tmp_path / "debug")
    assert result["end_confirmed"] and not engine.calls
    probes = [details for event, details in engine.events if event == "guide_overlay_wait"]
    assert len(probes) == 2 and all(details["read_only"] for details in probes)
    assert any(event == "guide_overlay_dismissed" and details["method"] == "read_only_wait" for event, details in engine.events)


@pytest.mark.parametrize("brand", ["小荷AI医生", "其他小程序"])
def test_settled_frame_checks_ocr_only_miniapp_brand_before_rejecting_the_page(brand):
    from geo_collector.platforms import douyin

    class Phone(FakeDevice):
        def __init__(self):
            super().__init__(references=False)
            self.package, self.recognitions = douyin.PACKAGE, 0

        def observe(self, ocr=False):
            current = super().observe(ocr)
            current.activity, current.texts = "MiniAppHostActivity", []
            if ocr:
                self.recognize(current)
            return current

        def recognize(self, current):
            self.recognitions += 1
            current.texts = [{"text": brand, "confidence": .99, "bounds": [12, 10, 200, 35]}]
            return current.texts

    phone = Phone()
    capture, engine = fast_capture(phone)
    initial = phone.observe()
    capture.package = phone.package
    capture.geometry = initial.logical_size, initial.physical_size
    capture.viewport = answer_viewport(initial)
    capture.answer_page_check, capture.answer_page_error = douyin.answer_ready, douyin.ANSWER_PAGE_ERROR
    assert not douyin.answer_ready(initial)
    if brand == "小荷AI医生":
        assert douyin.answer_ready(capture._settled(initial))
    else:
        with pytest.raises(CaptureError, match="不是小程序页面"):
            capture._settled(initial)
    assert phone.recognitions > 0
    assert not phone.swipes and not engine.calls
