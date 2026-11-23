"""Recognize MiniApp floating buttons without cutting ordinary answer pixels."""
from copy import deepcopy
from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import pytest

from geo_collector.capture import answer_viewport, capture_viewport
from geo_collector.device import map_bounds
from geo_collector.images import crop
from test_douyin_direct import DirectPhone


def scene(scale=1, *, shape="down", background="white", include=True):
    phone = DirectPhone(scale=scale, stage="answer")
    phone.question, phone.marker = "本次完整问题", False
    observation = phone.observe()
    if not include:
        return observation
    box = (320, 1230, 400, 1310)
    icon = (342, 1252, 378, 1288)
    pixels = Image.open(BytesIO(observation.png)).convert("RGB")
    shadow = Image.new("RGBA", pixels.size)
    scaled = lambda b: tuple(round(value * scale) for value in b)
    ImageDraw.Draw(shadow).ellipse(scaled((316, 1226, 404, 1314)), fill=(60, 60, 60, 140))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12 * scale))
    pixels.paste(shadow, (0, 0), shadow)
    draw = ImageDraw.Draw(pixels)
    draw.ellipse(scaled(box), fill=background)
    x0, y0, x1, y1 = icon
    w, h = x1 - x0, y1 - y0
    if shape == "down":
        points = [(x0 + w * .15, y0 + h * .32), (x0 + w * .5, y0 + h * .72), (x0 + w * .85, y0 + h * .32)]
    elif shape == "up":
        points = [(x0 + w * .15, y0 + h * .72), (x0 + w * .5, y0 + h * .32), (x0 + w * .85, y0 + h * .72)]
    elif shape == "line":
        points = [(x0 + w * .15, y0 + h * .32), (x0 + w * .85, y0 + h * .72)]
    else:
        points = []
    if points:
        draw.line([scaled(p) for p in points], fill=(22, 26, 34), width=max(2, round(4 * scale)))
    data = BytesIO()
    pixels.save(data, "PNG")
    observation.png = data.getvalue()
    for cls, bounds in [(cls, box) for cls in ("ScrollView", "HorizontalScrollView", "LinearLayout")] + [("ImageView", icon)]:
        observation.nodes.append({"text": "", "description": "", "resource_id": "", "class_name": "android.widget." + cls,
                                  "bounds": bounds, "clickable": False, "scrollable": False, "enabled": True,
                                  "focused": False, "package": observation.package})
    return observation


@pytest.mark.parametrize("scale", [.75, 1, 1.5, 2])
def test_direct_button_is_excluded_with_its_shadow_at_actual_pixel_scale(scale):
    observation = scene(scale)
    original = observation.png
    base = answer_viewport(observation)
    bounds, evidence = capture_viewport(observation, base)
    assert bounds == (*base[:3], round(1190 * scale))
    assert evidence["excluded_controls"] == [list(map_bounds((320, 1230, 400, 1310), observation.logical_size, observation.physical_size))]
    assert observation.png == original
    raw = np.asarray(Image.open(BytesIO(original)).convert("RGB"))
    captured = np.asarray(Image.open(BytesIO(crop(original, bounds))).convert("RGB"))
    assert np.array_equal(captured, raw[bounds[1]:bounds[3], bounds[0]:bounds[2]])


@pytest.mark.parametrize("change", ["other_app", "other_activity", "no_brand", "missing_container", "up", "line", "empty", "green"])
def test_direct_button_needs_page_identity_structure_and_real_white_down_icon(change):
    observation = scene(shape=change if change in {"up", "line", "empty"} else "down",
                        background="#28c8a0" if change == "green" else "white")
    base = answer_viewport(observation)
    if change == "other_app":
        observation.package = "example.app"
    elif change == "other_activity":
        observation.activity = "MainActivity"
    elif change == "no_brand":
        observation.texts = [line for line in observation.texts if line["text"] != "小荷AI医生"]
    elif change == "missing_container":
        observation.nodes = [node for node in observation.nodes if node["class_name"] != "android.widget.HorizontalScrollView"]
    bounds, evidence = capture_viewport(observation, base)
    assert bounds == base and evidence["excluded_controls"] == []


def test_button_disappearing_at_bottom_restores_the_full_original_frame():
    before, after = scene(), scene(include=False)
    base = answer_viewport(before)
    short, _ = capture_viewport(before, base)
    full, evidence = capture_viewport(after, base)
    assert short[3] < full[3] == base[3]
    assert not evidence["excluded_controls"]
    assert crop(after.png, full) == crop(after.png, base)


def test_repeated_candidate_nodes_do_not_duplicate_the_same_button():
    observation = scene()
    observation.nodes += deepcopy(observation.nodes[-4:])
    bounds, evidence = capture_viewport(observation, answer_viewport(observation))
    assert bounds[3] == 1190 and len(evidence["excluded_controls"]) == 1
