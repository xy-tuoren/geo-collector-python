from io import BytesIO

from PIL import Image
import pytest

from geo_collector.device import Observation
from geo_collector.rules import LocateError, infer_target, locate, validate_recipe


def observation(physical, logical, *, text="参考3篇医学文献", bounds=None, node_id=""):
    stream = BytesIO()
    Image.new("RGB", physical, "white").save(stream, format="PNG")
    box = bounds or [physical[0] * .15, physical[1] * .25, physical[0] * .65, physical[1] * .28]
    logical_box = [v * logical[i % 2] / physical[i % 2] for i, v in enumerate(box)]
    return Observation(stream.getvalue(), "<hierarchy/>", "com.example.collector", "MainActivity", logical, physical, "1",
                       [{"text": text, "resource_id": node_id, "bounds": logical_box}],
                       [{"text": text, "confidence": .99, "bounds": box}])


@pytest.mark.parametrize("physical,logical", [((1080, 2400), (720, 1600)), ((720, 1600), (1080, 2400))])
def test_dynamic_anchor_is_learned_as_pattern_and_reused_after_text_and_position_change(tmp_path, physical, logical):
    pattern = r"^参考\d+篇医学文献$"
    before = observation(physical, logical)
    b = before.texts[0]["bounds"]
    point = (b[0] + (b[2] - b[0]) * .9, (b[1] + b[3]) / 2)
    target = infer_target(before, point, tmp_path, hints=[pattern])
    assert target["kind"] == "ocr" and target["pattern"] == pattern

    # The reference count, position, font width and screenshot/UI scale change.
    next_physical, next_logical = logical, physical
    box = [next_physical[0] * .12, next_physical[1] * .35, next_physical[0] * .75, next_physical[1] * .39]
    after = observation(next_physical, next_logical, text="参考12篇医学文献", bounds=box)
    expected = ((box[0] + (box[2] - box[0]) * .9) * next_logical[0] / next_physical[0],
                (box[1] + box[3]) / 2 * next_logical[1] / next_physical[1])
    assert locate(target, after, tmp_path) == pytest.approx(expected)
    assert "参考3篇医学文献" not in str(target)


@pytest.mark.parametrize("physical,logical", [((1080, 2400), (720, 1600)), ((720, 1600), (1080, 2400))])
def test_stable_resource_selector_uses_logical_bounds_and_is_relocated_each_time(tmp_path, physical, logical):
    before = observation(physical, logical, text="搜索", node_id="com.example.collector:id/search")
    b = before.texts[0]["bounds"]
    target = infer_target(before, ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), tmp_path)
    assert target["kind"] == "node" and target["attr"] == "resource_id"
    after = observation(physical, logical, text="搜索", node_id="com.example.collector:id/search",
                        bounds=[physical[0] * .2, physical[1] * .4, physical[0] * .8, physical[1] * .46])
    assert locate(target, after, tmp_path) == pytest.approx((logical[0] * .5, logical[1] * .43))


def test_duplicate_anchor_is_rejected_until_context_makes_it_unique(tmp_path):
    current = observation((360, 800), (720, 1600), text="查看全文")
    current.texts.append({"text": "查看全文", "confidence": .99, "bounds": [54, 600, 234, 624]})
    target = {"kind": "ocr", "pattern": "^查看全文$"}
    with pytest.raises(LocateError, match="匹配 2"):
        locate(target, current, tmp_path)
    assert locate({**target, "region": [0, 0, 1, .5]}, current, tmp_path) == pytest.approx((288, 424))


@pytest.mark.parametrize("plan", [
    {"actions": [{"type": "tap", "target": {"kind": "point", "point": [100, 200]}}]},
    {"actions": [{"type": "input", "text": "arbitrary question"}]},
    {"actions": [{"type": "send"}]},
    {"actions": [{"type": "back"}], "success": "always"},
])
def test_repairs_cannot_supply_raw_coordinates_input_or_custom_success_rules(plan):
    with pytest.raises(ValueError):
        validate_recipe(plan)


def test_ocr_offset_cannot_turn_a_valid_anchor_into_an_out_of_screen_tap(tmp_path):
    current = observation((360, 800), (720, 1600))
    target = {"kind": "ocr", "pattern": r"^参考\d+篇医学文献$", "offset": [3, .5]}
    with pytest.raises(LocateError, match="越过屏幕"):
        locate(target, current, tmp_path)


@pytest.mark.parametrize("distance", [.01, .81, float("nan"), float("inf"), "0.2", None])
def test_anchored_swipe_rejects_invalid_screen_distance(distance):
    plan = {"actions": [{"type": "swipe", "direction": "down", "distance_fraction": distance,
                         "target": {"kind": "ocr", "pattern": "^参考药品$"}}]}
    with pytest.raises(ValueError, match="滑动距离"):
        validate_recipe(plan)


@pytest.mark.parametrize("target", [
    {"kind": "point", "point": [100, 200]},
    {"kind": "ocr", "pattern": "^参考药品$", "region": [0, .7, 1, .2]},
])
def test_swipe_targets_receive_the_same_anchor_validation_as_taps(target):
    with pytest.raises(ValueError):
        validate_recipe({"actions": [{"type": "swipe", "direction": "down", "target": target}]})


def test_obfuscated_id_and_full_width_container_fall_back_to_unique_visual_template(tmp_path):
    from PIL import ImageDraw
    from geo_collector.rules import generalize_region, is_obfuscated_id

    assert is_obfuscated_id("com.ss.android.ugc.aweme:id/sgk")
    assert is_obfuscated_id("com.ss.android.ugc.aweme:id/2vk")
    assert is_obfuscated_id("com.ss.android.ugc.aweme:id/nl=")
    assert not is_obfuscated_id("com.ss.android.ugc.aweme:id/et_search_kw")
    assert not is_obfuscated_id("android:id/content")

    image = Image.new("RGB", (1080, 2400), "white")
    draw = ImageDraw.Draw(image)
    # Draw a unique speech-bubble-plus icon at (629, 165) inside a full-width R8 toolbar.
    draw.rounded_rectangle((604, 140, 654, 186), radius=8, outline="black", width=4)
    draw.line((629, 151, 629, 175), fill="black", width=4)
    draw.line((617, 163, 641, 163), fill="black", width=4)
    stream = BytesIO()
    image.save(stream, format="PNG")
    scene = Observation(
        stream.getvalue(), "<hierarchy/>", "com.ss.android.ugc.aweme", "MiniAppHostActivity0",
        (1080, 2400), (1080, 2400), "40.4.0",
        nodes=[
            {"package": "com.ss.android.ugc.aweme", "resource_id": "com.ss.android.ugc.aweme:id/sgk",
             "text": "", "description": "", "bounds": [0, 0, 1080, 237]},
            {"package": "com.ss.android.ugc.aweme", "resource_id": "",
             "text": "", "description": "", "bounds": [0, 96, 825, 234]},
        ],
        texts=[],
    )
    preview_target = infer_target(scene, (629, 165), tmp_path / "assets", save_assets=False)
    assert preview_target["kind"] == "template"
    assert not (tmp_path / "assets").exists()

    saved_target = infer_target(scene, (629, 165), tmp_path / "assets", save_assets=True)
    assert saved_target["kind"] == "template"
    assert locate(saved_target, scene, tmp_path / "assets") == pytest.approx((629, 165), abs=2)

    # A second identical icon in the search region makes the visual patch ambiguous.
    draw.rounded_rectangle((704, 140, 754, 186), radius=8, outline="black", width=4)
    draw.line((729, 151, 729, 175), fill="black", width=4)
    draw.line((717, 163, 741, 163), fill="black", width=4)
    dup_stream = BytesIO()
    image.save(dup_stream, format="PNG")
    dup_scene = Observation(dup_stream.getvalue(), scene.xml, scene.package, scene.activity,
                            scene.logical_size, scene.physical_size, scene.version, scene.nodes, [])
    with pytest.raises(ValueError, match="不唯一"):
        infer_target(dup_scene, (629, 165), tmp_path / "dup", save_assets=False)

    # Region generalization drops region for unique stable IDs and widens header OCR safely.
    obs_dict = {
        "package": "app.demo", "logical_size": [1080, 2400], "physical_size": [1080, 2400],
        "nodes": [{"package": "app.demo", "resource_id": "app.demo:id/search_btn", "bounds": [920, 80, 1040, 160]}],
        "texts": [{"text": "新会话", "confidence": .99, "bounds": [600, 100, 720, 160]},
                  {"text": "查看全文", "confidence": .99, "bounds": [80, 600, 260, 650]},
                  {"text": "查看全文", "confidence": .99, "bounds": [80, 1400, 260, 1450]}],
    }
    widened_id = generalize_region({"kind": "node", "attr": "resource_id", "value": "app.demo:id/search_btn",
                                    "region": [.84, .03, .97, .07]}, obs_dict, (920, 80, 1040, 160))
    assert "region" not in widened_id
    widened_ocr = generalize_region({"kind": "ocr", "pattern": "^新会话$", "region": [.55, .04, .67, .07]},
                                    obs_dict, (600, 100, 720, 160))
    assert widened_ocr["region"] == [0, 0, 1, .22]
    disambiguated = generalize_region({"kind": "ocr", "pattern": "^查看全文$", "region": [.07, .24, .25, .28]},
                                      obs_dict, (80, 600, 260, 650))
    assert disambiguated["region"][3] < .58


def test_nested_duplicate_button_wrappers_collapse_while_distinct_nodes_stay_ambiguous(tmp_path):
    from geo_collector.rules import generalize_region

    base = observation((1080, 2400), (1080, 2400), text="")
    # Real Douyin 40.4.0 top-right Search button: clickable outer wrapper + inner icon node both have description="搜索".
    nested_scene = Observation(
        base.png, base.xml, "com.ss.android.ugc.aweme", "com.ss.android.ugc.aweme.splash.SplashActivity",
        (1080, 2400), (1080, 2400), "40.4.0",
        nodes=[
            {"package": "com.ss.android.ugc.aweme", "text": "", "description": "",
             "resource_id": "com.ss.android.ugc.aweme:id/2ss", "class_name": "android.widget.ImageView",
             "bounds": [31, 108, 119, 196], "clickable": False},
            {"package": "com.ss.android.ugc.aweme", "text": "", "description": "搜索",
             "resource_id": "", "class_name": "android.widget.Button",
             "bounds": [935, 92, 1078, 213], "clickable": True},
            {"package": "com.ss.android.ugc.aweme", "text": "", "description": "搜索",
             "resource_id": "com.ss.android.ugc.aweme:id/2ss", "class_name": "android.widget.Button",
             "bounds": [957, 108, 1045, 196], "clickable": False},
        ],
        texts=[],
    )
    target = {"kind": "node", "attr": "description", "value": "搜索"}
    assert locate(target, nested_scene, tmp_path) == pytest.approx((1001.0, 152.0))
    widened = generalize_region({**target, "region": [.86, .03, .99, .09]}, nested_scene, (935, 92, 1078, 213))
    assert widened["region"] == [0, 0, 1, .22]
    inferred = infer_target(nested_scene, (1004, 144), tmp_path, save_assets=False)
    assert inferred["kind"] == "node" and inferred["attr"] == "description" and inferred["value"] == "搜索"

    # Disjoint nodes sharing the same resource_id (top-left sidebar + top-right search) remain ambiguous.
    with pytest.raises(LocateError, match="匹配 2"):
        locate({"kind": "node", "attr": "resource_id", "value": "com.ss.android.ugc.aweme:id/2ss"}, nested_scene, tmp_path)

    # A full-screen container wrapping a small button must NOT collapse.
    huge_wrapper = Observation(
        base.png, base.xml, "com.example.app", "Main", (1080, 2400), (1080, 2400), "1.0",
        nodes=[
            {"package": "com.example.app", "description": "搜索", "bounds": [0, 0, 1080, 2400]},
            {"package": "com.example.app", "description": "搜索", "bounds": [957, 108, 1045, 196]},
        ],
        texts=[],
    )
    with pytest.raises(LocateError, match="匹配 2"):
        locate(target, huge_wrapper, tmp_path)

    # Two sibling buttons inside one container must NOT collapse.
    siblings = Observation(
        base.png, base.xml, "com.example.app", "Main", (1080, 2400), (1080, 2400), "1.0",
        nodes=[
            {"package": "com.example.app", "description": "搜索", "bounds": [800, 90, 1070, 210]},
            {"package": "com.example.app", "description": "搜索", "bounds": [810, 100, 910, 200]},
            {"package": "com.example.app", "description": "搜索", "bounds": [950, 100, 1050, 200]},
        ],
        texts=[],
    )
    with pytest.raises(LocateError, match="匹配 3"):
        locate(target, siblings, tmp_path)
