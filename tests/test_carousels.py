from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
import pytest

from geo_collector.carousels import ProductCarousels, horizontal_overlap, product_carousels, verify_carousels
from geo_collector.capture import answer_viewport, citation_state, direct_question_bubble, question_bubble, session_boundary
from geo_collector.device import Observation, parse_nodes
from geo_collector.images import CaptureError, _png, crop, stable
from geo_collector.recovery import save_observation
from test_capture import FakeDevice


class CarouselDevice:
    package = "example.miniapp"

    def __init__(self, scale=1, logical_scale=1):
        self.scale, self.logical_scale = scale, logical_scale
        self.offset = 0
        self.row = (20, 300, 340, 480)
        self.viewport = (0, 100, 360, 760)
        self.strip = Image.fromarray(np.random.default_rng(13).integers(0, 255, (180, 960, 3), dtype=np.uint8))
        self.outside = Image.fromarray(np.random.default_rng(17).integers(0, 255, (800, 360, 3), dtype=np.uint8))
        self.swipes = []
        self.interrupt = None

    def observe(self):
        image = self.outside.copy()
        image.paste(self.strip.crop((self.offset, 0, self.offset + 320, 180)), (20, 300))
        image = image.resize((round(360 * self.scale), round(800 * self.scale)), Image.Resampling.NEAREST)
        def bounds(box):
            a, b, c, d = [round(x * self.logical_scale) for x in box]
            return f"[{a},{b}][{c},{d}]"
        xml = (f'<hierarchy><node class="androidx.recyclerview.widget.RecyclerView" package="{self.package}" bounds="{bounds(self.viewport)}">'
               f'<node class="android.widget.HorizontalScrollView" scrollable="true" package="{self.package}" bounds="{bounds(self.row)}" />'
               '</node></hierarchy>')
        return Observation(_png(image), xml, self.package, "MiniAppHostActivity0", (round(360 * self.logical_scale), round(800 * self.logical_scale)), image.size, "1", parse_nodes(xml), [])

    def swipe(self, start, end, duration=.4, *, expected_package):
        assert expected_package == self.package
        assert start[1] == end[1]
        self.swipes.append((start, end))
        restore = end[0] > start[0]
        self.offset = max(0, min(640, self.offset + round((start[0] - end[0]) / self.logical_scale)))
        if self.interrupt == ("restore" if restore else "forward"):
            self.interrupt = None
            raise RuntimeError("interrupted after dispatch")


class CarouselCapture:
    def __init__(self, tmp_path, device):
        self.device, self.package = device, device.package
        self.attempt = tmp_path / "debug"
        self.attempt.mkdir(exist_ok=True)
        self.viewport = tuple(round(x * device.scale) for x in device.viewport)
        item = device.observe()
        self.geometry = (item.logical_size, item.physical_size)
        self.counter = 0
        self.engine = self
        self.events = []

    def log(self, event, **details):
        self.events.append((event, details))

    def _checkpoint_products(self, checkpoint):
        self.checkpoint = deepcopy(checkpoint)

    def _save(self, label, observation):
        self.counter += 1
        path = self.attempt / f"{self.counter:03d}-{label}"
        save_observation(path, observation)
        return str(path)

    def _settled(self):
        return self.device.observe()

    def _drawer_ready(self, observation):
        return observation, [tuple(round(x * self.device.scale) for x in self.device.row)]


def export_rows(collector, tmp_path):
    directory = tmp_path / "delivery"
    directory.mkdir(exist_ok=True)
    paths = []
    for index, source in enumerate(collector.image_files()):
        path = directory / f"horizontal-{index}.png"
        path.write_bytes(Path(source).read_bytes())
        paths.append(str(path))
    collector.state["image_paths"] = paths
    return directory


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
def test_carousel_captures_every_specification_and_restores_list(tmp_path, scale, logical_scale):
    phone = CarouselDevice(scale, logical_scale)
    capture = CarouselCapture(tmp_path, phone)
    initial = phone.observe()
    collector = ProductCarousels(capture, {})
    result = collector.capture_visible(initial, final=True)
    assert phone.offset == 0 and stable(initial.png, result.png)
    row = collector.state["rows"][0]
    assert row["complete"] and len(row["frame_files"]) > 2 and len(row["end_observations"]) == 2
    assert all(seam["verified"] and seam["axis"] == "horizontal" for seam in row["seams"])
    assert all(start[1] == end[1] for start, end in phone.swipes)
    delivery = export_rows(collector, tmp_path)
    verify_carousels(collector.state, capture.attempt, delivery)
    before = len(phone.swipes)
    collector.capture_visible(result, final=True)
    assert len(phone.swipes) == before  # Overlapping vertical viewports do not re-capture a known row.


@pytest.mark.parametrize("phase", ["forward", "restore"])
def test_carousel_resume_verifies_unknown_swipe_before_continuing(tmp_path, phase):
    phone = CarouselDevice()
    capture = CarouselCapture(tmp_path, phone)
    checkpoint = {}
    collector = ProductCarousels(capture, checkpoint)
    phone.interrupt = phase
    with pytest.raises(RuntimeError, match="after dispatch"):
        collector.capture_visible(phone.observe())
    assert checkpoint["horizontal"]["active"] is not None
    resumed = ProductCarousels(capture, deepcopy(capture.checkpoint))
    resumed.capture_visible(phone.observe(), final=True)
    assert phone.offset == 0
    delivery = export_rows(resumed, tmp_path)
    verify_carousels(resumed.state, capture.attempt, delivery)


def test_only_nested_horizontal_lists_are_captured(tmp_path):
    phone = CarouselDevice()
    capture = CarouselCapture(tmp_path, phone)
    item = phone.observe()
    root = ET.fromstring(item.xml)
    row = root.find(".//node[@class='android.widget.HorizontalScrollView']")
    parent = root.find(".//node[@class='androidx.recyclerview.widget.RecyclerView']")
    parent.remove(row)
    root.append(row)
    item.xml = ET.tostring(root, encoding="unicode")
    assert product_carousels(item, capture.viewport) == []
    collector = ProductCarousels(capture, {})
    collector.capture_visible(item, final=True)
    assert not phone.swipes


@pytest.mark.parametrize("row,final,raises", [((20, 650, 340, 830), False, False), ((20, 650, 340, 830), True, True), ((20, 50, 340, 230), False, True)])
def test_clipped_carousel_waits_for_whole_row_or_fails(tmp_path, row, final, raises):
    phone = CarouselDevice()
    phone.row = row
    capture = CarouselCapture(tmp_path, phone)
    collector = ProductCarousels(capture, {})
    if raises:
        with pytest.raises(CaptureError, match="未完整进入视口"):
            collector.capture_visible(phone.observe(), final=final)
    else:
        collector.capture_visible(phone.observe(), final=final)
    assert not phone.swipes


def test_horizontal_overlap_rejects_blank_unrelated_or_tampered_pixels():
    empty = _png(Image.new("RGB", (320, 180), "white"))
    with pytest.raises(CaptureError):
        horizontal_overlap(empty, empty)
    phone = CarouselDevice()
    first = crop(phone.observe().png, phone.row)
    phone.offset = 128
    second = crop(phone.observe().png, phone.row)
    assert horizontal_overlap(first, second)["overlap"] == 192
    with pytest.raises(CaptureError):
        horizontal_overlap(first, _png(Image.fromarray(np.random.default_rng(99).integers(0, 255, (180, 320, 3), dtype=np.uint8))))


@pytest.mark.parametrize("mutation", ["end", "restore", "pixel"])
def test_carousel_verification_rejects_missing_end_or_broken_restoration(tmp_path, mutation):
    phone = CarouselDevice()
    capture = CarouselCapture(tmp_path, phone)
    collector = ProductCarousels(capture, {})
    collector.capture_visible(phone.observe(), final=True)
    delivery = export_rows(collector, tmp_path)
    row = collector.state["rows"][0]
    if mutation == "end":
        row["end_observations"] = row["end_observations"][:1]
    elif mutation == "restore":
        row["restore_observation"] = row["frame_observations"][-1]
    else:
        Path(collector.state["image_paths"][0]).write_bytes(_png(Image.new("RGB", (320, 180), "white")))
    with pytest.raises(CaptureError):
        verify_carousels(collector.state, capture.attempt, delivery)


@pytest.mark.parametrize("scale,count", [(1, "5"), (2, "五"), (1, "2篇指南及1")])
def test_two_citation_previews_are_not_an_expanded_panel(scale, count):
    item = FakeDevice(scale).observe()
    def line(text, box):
        return {"text": text, "confidence": .99, "bounds": tuple(round(x * scale) for x in box)}
    item.texts = [line(f"参考{count}篇文献", (24, 230, 270, 250)),
                  line("1.腹泻指南", (24, 257, 270, 275)),
                  line("2.临床共识", (24, 282, 270, 300)),
                  line("腹泻脱水处理建议", (24, 350, 270, 370))]
    viewport = answer_viewport(item)
    assert not citation_state(item, viewport)["expanded"]
    item.texts[-1] = line("3.第三篇文献", (24, 310, 270, 330))
    assert citation_state(item, viewport)["expanded"]


def test_preview_protection_also_applies_to_learned_reference_roles():
    from geo_collector.goals import bind_roles
    # The learned path is covered using the same real OCR rows and role nodes.
    item = FakeDevice().observe()
    labels = [("参考5篇文献", (24, 230, 270, 250)), ("1.腹泻指南", (24, 257, 270, 275)),
              ("2.临床共识", (24, 282, 270, 300)), ("正文开始", (24, 350, 270, 370))]
    item.texts = [{"text": text, "confidence": .99, "bounds": box} for text, box in labels]
    item.nodes.extend({"text": text, "bounds": box, "package": item.package, "class_name": "android.widget.TextView", "resource_id": f"id/{index}"}
                      for index, (text, box) in enumerate(labels))
    item = bind_roles({"perception": {"citations_header": {"kind": "node", "attr": "resource_id", "value": "id/0"},
                                      "citations_rows": {"kind": "node", "attr": "resource_id", "value": "id/1"}}}, item)
    assert not citation_state(item, answer_viewport(item))["expanded"]


@pytest.mark.parametrize("scale", [1, 2])
def test_direct_same_question_only_matches_after_latest_session_divider(scale):
    from PIL import ImageDraw
    from geo_collector.images import _image
    item = FakeDevice(scale).observe()
    item.package = "com.ss.android.ugc.aweme"
    item.activity = "com.tt.miniapphost.placeholder.MiniAppHostActivity0"
    for node in item.nodes:
        node["package"] = item.package
    image = _image(item.png).copy()
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(x * scale for x in (90, 350, 340, 430)), 22 * scale, fill="#00bdb2")
    item.png = _png(image)
    item.texts.extend([{"text": "已开始新会话", "confidence": .99, "bounds": tuple(x * scale for x in (120, 300, 240, 320))},
                       {"text": "Example full question", "confidence": .99, "bounds": tuple(x * scale for x in (115, 370, 320, 394))}])
    viewport = answer_viewport(item)
    boundary = session_boundary(item, viewport)
    assert boundary
    target = question_bubble(item, "Example full question", viewport)
    assert target and target["bounds"][1] > boundary[3]
    item.texts = [line for line in item.texts if line["bounds"][1] != 370 * scale]
    assert question_bubble(item, "Example full question", viewport) is None


def test_direct_capture_never_scrolls_past_new_session_to_old_same_question(tmp_path):
    from test_capture import fast_capture
    phone = FakeDevice()
    capture, engine = fast_capture(phone)
    item = phone.observe()
    item.package = phone.package = "com.ss.android.ugc.aweme"
    item.activity = "com.tt.miniapphost.placeholder.MiniAppHostActivity0"
    item.texts.append({"text": "已开始新会话", "confidence": .99, "bounds": (120, 300, 240, 320)})
    for node in item.nodes:
        node["package"] = item.package
    capture.viewport = answer_viewport(item)
    capture.package = item.package
    engine.state["entry"] = "douyin-direct"
    # A direct capture now refuses unproven send/tail identity before looking
    # at history. The visible empty-session boundary also independently rejects
    # the old same-question bubble above it.
    with pytest.raises(CaptureError, match="缺少本次发送证据或最新回答末尾证据"):
        capture._top(item, phone.question)
    with pytest.raises(CaptureError, match="不能继续进入旧会话"):
        direct_question_bubble(item, phone.question, capture.viewport)
    assert not phone.swipes


def test_product_card_matches_wrapped_single_char_ocr_continuation():
    from PIL import ImageDraw
    from geo_collector.capture import drawer_state, product_image_evidence
    from geo_collector.device import Observation

    image = Image.new("RGB", (1080, 2400), "white")
    draw = ImageDraw.Draw(image)
    # Draw colorful medicine box texture inside the photo region of card (211, 727, 618, 1108)
    for y in range(740, 1090, 12):
        draw.rectangle((240, y, 590, y + 6), fill=(20, 160, 120))
    png_bytes = _png(image)
    nodes = [
        {"text": "", "description": "", "resource_id": "", "class_name": "androidx.recyclerview.widget.RecyclerView",
         "bounds": (0, 614, 1080, 2356), "clickable": False, "scrollable": True, "package": "com.ss.android.ugc.aweme",
         "focused": False, "enabled": True},
        {"text": "", "description": "", "resource_id": "", "class_name": "android.widget.HorizontalScrollView",
         "bounds": (33, 727, 1047, 1508), "clickable": False, "scrollable": True, "package": "com.ss.android.ugc.aweme",
         "focused": False, "enabled": True},
        {"text": "", "description": "", "resource_id": "", "class_name": "android.view.ViewGroup",
         "bounds": (211, 727, 618, 1508), "clickable": False, "scrollable": False, "package": "com.ss.android.ugc.aweme",
         "focused": False, "enabled": True},
    ]
    texts = [
        {"text": "参考药品", "confidence": 0.99, "bounds": (441, 511, 637, 573)},
        {"text": "帕夫林·白芍总苷胶囊", "confidence": 0.998, "bounds": (32, 641, 425, 686)},
        {"text": "帕夫林·白芍总苷胶", "confidence": 0.999, "bounds": (237, 1117, 591, 1163)},
        # Isolated wrapped CJK tail character commonly gets ~0.73-0.75 confidence in RapidOCR.
        {"text": "囊", "confidence": 0.733, "bounds": (233, 1170, 289, 1227)},
        {"text": "查看说明书", "confidence": 0.99, "bounds": (335, 1408, 495, 1450)},
    ]
    obs = Observation(png_bytes, "<hierarchy/>", "com.ss.android.ugc.aweme",
                      "com.tt.miniapphost.placeholder.MiniAppHostActivity0",
                      (1080, 2400), (1080, 2400), "40.4.0", nodes, texts)
    ds = drawer_state(obs)
    assert ds is not None
    evidence = product_image_evidence(obs, ds["viewport"])
    assert evidence["ready"] is True
    assert evidence["regions"] == [(211, 727, 618, 1108)]
