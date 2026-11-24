"""Named Douyin Xiaohe mini-app: discover once, open a new session, send once.

Navigation methods are candidates checked against the current page.  Unknown
layouts use the normal StepEngine handoff; neither learned navigation nor an
uncertain send can bypass the exact question and session evidence.
"""
from __future__ import annotations

import re
import hashlib
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import numpy as np

from ..capture import (AnswerCapture, _answer_content_visible, answer_viewport, direct_question_bubble,
                       direct_scroll_seam, drawer_state, guide_visible, latest_user_bubble, question_bubble)
from ..device import DeviceError, map_bounds, validate_geometry
from ..goals import BEFORE_SEND, SESSION_HINTS, role_nodes
from ..images import CaptureError, _image, stable
from ..recovery import load_observation, save_observation
from . import douyin
from .welcome import card_signature, tail_rows, welcome_fault
from .common import edit, exact, label_taps, nodes, tap


ID = "douyin-direct"
LABEL = "抖音小荷小程序"
PACKAGE = douyin.PACKAGE
QUERY = "小荷ai医生 小程序"
REENTER_SUPPORTED = False
ENTRY_STEP = None
INPUT_HINTS = ("发送消息|输入消息|输入问题",)
SEND_HINTS = ("发送",)
ANSWER_PAGE_ERROR = "当前不是抖音小荷小程序的对话页，不能继续采集"
_BRAND = re.compile(r"^[<‹〈く]?小荷AI医生$", re.I)
_INPUT = {"发送消息", "输入消息", "输入问题"}


def _box_valid(box, size):
    return len(box) == 4 and 0 <= box[0] < box[2] <= size[0] and 0 <= box[1] < box[3] <= size[1]


def _contains(outer, inner):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _visible(observation):
    return [n for n in nodes(observation) if _box_valid(n["bounds"], observation.logical_size)]


def _labels(observation):
    """Deduplicate native/OCR labels while retaining a usable semantic anchor."""
    result = []
    width, height = observation.logical_size
    seen = set()
    for node in _visible(observation):
        label = node.get("text") or node.get("description")
        key = exact(label or ""), tuple(node["bounds"])
        if label and key not in seen:
            seen.add(key)
            result.append({"label": exact(label), "bounds": node["bounds"], "node": node})
    for line in observation.texts:
        if line.get("confidence", 0) < .85 or not _box_valid(line["bounds"], observation.physical_size):
            continue
        label = exact(line["text"])
        box = map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
        def same_label(item):
            if item["label"].casefold() != label.casefold():
                return False
            native = item["bounds"]
            intersection = max(0, min(native[2], box[2]) - max(native[0], box[0])) * max(0, min(native[3], box[3]) - max(native[1], box[1]))
            smaller = min((native[2] - native[0]) * (native[3] - native[1]), (box[2] - box[0]) * (box[3] - box[1]))
            # Native accessibility may label a whole header row while OCR sees
            # only its text.  Count that overlapping header once, not two cards.
            return (all(abs(a - b) <= (height if i % 2 else width) * .012 for i, (a, b) in enumerate(zip(native, box)))
                    or intersection >= smaller * .7 and abs(sum(native[1::2]) - sum(box[1::2])) <= height * .03)
        if not any(same_label(item) for item in result):
            result.append({"label": label, "bounds": box, "ocr": line})
    return result


def _region(box, size, margin=.005):
    return [max(0, box[0] / size[0] - margin), max(0, box[1] / size[1] - margin),
            min(1, box[2] / size[0] + margin), min(1, box[3] / size[1] + margin)]


def _target(item, size, point=None):
    box = item["bounds"]
    if "node" in item:
        node = item["node"]
        attr = "resource_id" if node.get("resource_id") else "description" if node.get("description") else "text" if node.get("text") else "class_name"
        target = {"kind": "node", "attr": attr, "value": node[attr], "region": _region(box, size)}
    else:
        target = {"kind": "ocr", "pattern": "^" + re.escape(str(item["ocr"]["text"])) + "$", "region": _region(box, size)}
    if point is not None:
        offset = [(point[0] - box[0]) / (box[2] - box[0]), (point[1] - box[1]) / (box[3] - box[1])]
        if not all(-2 <= value <= 3 for value in offset):
            return None
        target["offset"] = offset
    return {"actions": [{"type": "tap", "target": target}]}


def _host(observation):
    try:
        validate_geometry(observation.logical_size, observation.physical_size)
    except DeviceError:
        return False
    w, h = observation.logical_size
    return observation.package == PACKAGE and any(
        n["bounds"][0] <= w * .03 and n["bounds"][1] <= h * .06
        and n["bounds"][2] >= w * .97 and n["bounds"][3] >= h * .9 for n in _visible(observation))


def _shell(observation):
    if (not _host(observation) or ("MiniAppHostActivity" not in observation.activity and not role_nodes(observation, "answer_page"))
            or edit(observation, "douyin")):
        return None
    w, h = observation.logical_size
    closes = [item for item in _labels(observation) if item["label"].casefold() in {"关闭", "close"}
              and item["bounds"][0] > w * .8 and 0 < item["bounds"][1] < item["bounds"][3] < h * .13
              and w * .04 <= item["bounds"][2] - item["bounds"][0] <= w * .16]
    return closes[0] if len(closes) == 1 else None


def _hint_fields(observation):
    # The shared Observation deliberately keeps a small node schema.  Hints
    # are read from the original XML here, never inferred from the question.
    fields = {}
    try:
        for element in ET.fromstring(observation.xml).iter("node"):
            if element.get("class") != "android.widget.EditText":
                continue
            values = re.findall(r"-?\d+", element.get("bounds", ""))
            if len(values) == 4:
                fields[tuple(map(int, values))] = {"hint": element.get("hint", ""), "password": element.get("password") == "true"}
    except ET.ParseError:
        return {}
    return fields


def chat_state(observation):
    close = _shell(observation)
    if close is None:
        return None
    w, h = observation.logical_size
    visible = _labels(observation)
    brands = [item for item in visible if _BRAND.fullmatch(item["label"])
              and item["bounds"][3] <= h * .12 and item["bounds"][2] < w * .52
              and abs(sum(item["bounds"][1::2]) / 2 - sum(close["bounds"][1::2]) / 2) < h * .035]
    if len(brands) != 1:
        return None
    hints = _hint_fields(observation)
    learned_inputs = {tuple(node["bounds"]) for node in role_nodes(observation, "input")}
    fields = []
    for node in _visible(observation):
        if not node.get("class_name", "").endswith("EditText"):
            continue
        detail = hints.get(tuple(node["bounds"]), {})
        hint = exact(node.get("hint") or detail.get("hint") or "")
        minimum = max(h * .25, close["bounds"][3] + h * .04) if node.get("focused") else h * .45
        if (node["bounds"][1] < minimum or node["bounds"][2] - node["bounds"][0] < w * .38
                or ((hint or exact(node.get("text", ""))) not in _INPUT and tuple(node["bounds"]) not in learned_inputs)
                or detail.get("password")):
            return None
        fields.append({**node, "hint": hint})
    voices = [item for item in visible if item["label"] == "按住说话" and item["bounds"][1] > h * .78
              and item["bounds"][0] > w * .25 and item["bounds"][2] < w * .75]
    if len(fields) > 1 or len(voices) > 1 or bool(fields) == bool(voices):
        return None
    return {"close": close, "brand": brands[0], "input": fields[0] if fields else None,
            "voice": voices[0] if voices else None}


def home_target(observation):
    if _shell(observation) is None:
        return None
    w, h = observation.logical_size
    visible = _labels(observation)
    brands = [v for v in visible if _BRAND.fullmatch(v["label"]) and v["bounds"][3] < h * .48]
    prompts = [v for v in visible if re.fullmatch(r"健康问题[,，]?随时问我|继续咨询AI医生", v["label"])
               and h * .3 < v["bounds"][1] < v["bounds"][3] < h * .75]
    home = [v for v in visible if v["label"] == "首页" and v["bounds"][1] > h * .8 and v["bounds"][2] < w * .6]
    mine = [v for v in visible if v["label"] == "我的" and v["bounds"][1] > h * .8 and v["bounds"][0] > w * .4]
    if not (len(brands) == len(prompts) == len(home) == len(mine) == 1 and brands[0]["bounds"][3] < prompts[0]["bounds"][1]):
        return None
    if prompts[0]["label"] == "继续咨询AI医生":
        # The returning-user home has a consultation card rather than the
        # initial prompt.  Keep both the branded header and its central greeting
        # so an unrelated recommendation or an App-download CTA cannot qualify.
        greetings = [v for v in visible if re.fullmatch(r"Hi我是小荷AI医生", v["label"], re.I)
                     and h * .2 < v["bounds"][1] < v["bounds"][3] < prompts[0]["bounds"][1]
                     and v["bounds"][0] < w * .6 and v["bounds"][2] > w * .35]
        if len(greetings) != 1 or brands[0]["bounds"][3] > h * .16:
            return None
    return prompts[0]


def named_entry_defaults(observation):
    if not _host(observation) or not douyin.search_results_ready(observation, QUERY):
        return []
    w, h = observation.logical_size
    visible = _labels(observation)
    brands = [v for v in visible if v["label"].casefold() == "小荷ai医生"]
    sections = [v for v in visible if v["label"] == "小程序"]
    actions = [v for v in visible if "node" in v and re.fullmatch(r"进入(?:按钮)?", v["label"])
               and v["bounds"][0] >= w * .6 and v["bounds"][2] - v["bounds"][0] <= w * .32
               and v["bounds"][3] - v["bounds"][1] <= h * .065]
    targets = {}
    editor = edit(observation, "douyin")
    for action in actions:
        button = action["bounds"]
        for node in sorted(_visible(observation), key=lambda n: (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1])):
            card = node["bounds"]
            if (card[1] < editor["bounds"][3] or card[2] - card[0] < w * .65
                    or card[3] - card[1] > h * .28 or not _contains(card, button)):
                continue
            tags = [v for v in sections if _contains(card, v["bounds"]) and v["bounds"][3] <= button[1]]
            eligible = [v for v in brands if _contains(card, v["bounds"]) and v["bounds"][2] < button[0]
                        and len(tags) == 1 and -h * .01 <= v["bounds"][1] - tags[0]["bounds"][3] <= h * .07
                        and abs(sum(v["bounds"][1::2]) / 2 - sum(button[1::2]) / 2) <= h * .045]
            if len(eligible) == 1:
                targets[tuple(button)] = _target(action, observation.logical_size)
                break
            if len(eligible) > 1:
                break
    return list(targets.values()) if len(targets) == 1 else []


def _named_card_evidence(observation):
    """Allow learned CTA labels while retaining the intended card identity."""
    if not _host(observation) or not douyin.search_results_ready(observation, QUERY):
        return False
    w, h = observation.logical_size
    values = _labels(observation)
    brands = [v["bounds"] for v in values if v["label"].casefold() == "小荷ai医生"]
    sections = [v["bounds"] for v in values if v["label"] == "小程序"]
    return any(card[2] - card[0] >= w * .65 and card[3] - card[1] <= h * .28
               and any(_contains(card, brand) for brand in brands)
               and any(_contains(card, section) for section in sections)
               for card in (node["bounds"] for node in _visible(observation)))


def dialog_target(observation):
    if _shell(observation) is None:
        return None
    h = observation.logical_size[1]
    values = _labels(observation)
    titles = [v for v in values if v["label"] == "开启新会话"]
    descriptions = [v for v in values if re.fullmatch(r"开启新会话后[,，]当前咨询将自动结束", v["label"])]
    buttons = [v for v in values if v["label"] == "确定"]
    matches = []
    for title in titles:
        for description in descriptions:
            for button in buttons:
                a, b, c = title["bounds"], description["bounds"], button["bounds"]
                if (h * .2 < a[1] < a[3] < h * .75 and 0 <= b[1] - a[3] <= h * .08
                        and 0 <= c[1] - b[3] <= h * .12 and c[3] < h * .85
                        and c[2] >= b[0] and c[0] <= b[2]):
                    matches.append(button)
    return matches[0] if len(matches) == 1 else None


def session_boundary(observation):
    if chat_state(observation) is None or dialog_target(observation):
        return None
    try:
        viewport = answer_viewport(observation)
        logical = map_bounds(viewport, observation.physical_size, observation.logical_size)
    except (CaptureError, DeviceError):
        return None
    w, h = observation.logical_size
    markers = [v for v in _labels(observation) if v["label"] == "已开始新会话"
               and logical[1] < v["bounds"][1] < v["bounds"][3] < logical[3] - h * .025
               and abs(sum(v["bounds"][::2]) / 2 - w / 2) < w * .08
               and v["bounds"][2] - v["bounds"][0] < w * .4]
    if not markers:
        return None
    marker = max(markers, key=lambda v: v["bounds"][3])
    tail = (logical[0], marker["bounds"][3] + h * .003, logical[2], logical[3])
    has_messages = any(v["bounds"][1] >= tail[1] and v["bounds"][1] < tail[3] for v in _labels(observation))
    return {"marker": marker["bounds"], "tail": tail, "physical_tail": map_bounds(tail, observation.logical_size, observation.physical_size),
            "has_messages": has_messages}


def reset_welcome_witness(before, after):
    """Prove this card appeared on reset, using unchanged history above a *new* divider.

    Both frames come from the collector's single foreground confirmation. A moved
    viewport with an already-existing divider must not enroll an old answer.
    """
    if ((before.package, before.activity, before.logical_size, before.physical_size) !=
            (after.package, after.activity, after.logical_size, after.physical_size)
            or not chat_state(before) or dialog_target(before) or drawer_state(before)):
        return None
    boundary = session_boundary(after)
    if not boundary or not boundary['has_messages']:
        return None
    try:
        old_view, new_view = answer_viewport(before), answer_viewport(after)
    except CaptureError:
        return None
    marker = map_bounds(boundary['marker'], after.logical_size, after.physical_size)
    def transcript(frame, viewport, bottom):
        return sorted((line for line in frame.texts if line.get('confidence', 0) >= .85
                       and _contains(viewport, line['bounds']) and line['bounds'][3] <= bottom),
                      key=lambda line: (line['bounds'][1], line['bounds'][0]))
    old = transcript(before, old_view, old_view[3])
    full = transcript(after, new_view, new_view[3])
    prior = [row for row in full if row['bounds'][3] < marker[1]]
    if len(old) < 2 or len(prior) < 2:
        return None
    anchors = [exact(row['text']) for row in old[-2:]]
    if min(map(len, anchors)) < 3 or '已开始新会话' in anchors or anchors != [exact(r['text']) for r in prior[-2:]]:
        return None
    for rows in (old, full):
        values = [exact(r['text']) for r in rows]
        if sum(values[i:i+2] == anchors for i in range(len(values)-1)) != 1:
            return None
    dy = prior[-1]['bounds'][3] - old[-1]['bounds'][3]
    tolerance = after.physical_size[0] * .012
    for a, b in zip(old[-2:], prior[-2:]):
        if any(abs(y - x - (dy if i % 2 else 0)) > tolerance for i, (x, y) in enumerate(zip(a['bounds'], b['bounds']))):
            return None
    projected = (marker[0], marker[1]-dy, marker[2], marker[3]-dy)
    if not _contains(old_view, projected) or projected[1] <= old[-1]['bounds'][3]:
        return None
    # This exact relative location was blank before the confirmed reset.
    cleared = np.asarray(_image(before.png).crop(projected), dtype=np.int16)
    marked = np.asarray(_image(after.png).crop(marker), dtype=np.int16)
    if cleared.min() < 230 or np.ptp(cleared) > 12 or (marked.min(axis=2) < 190).mean() < .015:
        return None
    return card_signature(tail_rows(_labels(after), boundary['tail']))


def _input_empty(editor):
    return editor is not None and str(editor.get("text") or "") in {"", editor.get("hint", ""), "发送消息", "输入消息", "输入问题"}


def _session_verdict(observation, question=""):
    chat, boundary = chat_state(observation), session_boundary(observation)
    if not chat or not boundary:
        return None, "缺少当前小荷对话页或最新会话分隔"
    if chat["input"] and not (_input_empty(chat["input"]) or chat["input"].get("text") == question):
        return None, "当前输入框包含其他草稿"
    try:
        if boundary["has_messages"]:
            fault = welcome_fault(observation, boundary, _labels(observation))
            return (None, fault) if fault else ("welcome", None)
        l, t, r, b = boundary["physical_tail"]
        pixels = np.asarray(_image(observation.png), dtype=np.int16)[t:b, l:r]
        background = np.median(pixels.reshape(-1, 3), axis=0)
        # The session tail must actually be empty, including content not exposed
        # by accessibility/OCR.  Unsupported decorated backgrounds are handed off.
        if (pixels.size and background.min() >= 235 and background.max() - background.min() < 12
                and np.mean(np.max(np.abs(pixels - background), axis=2) > 14) < .002):
            return "blank", None
        return None, "最新分隔与输入框之间仍有未识别的像素"
    except (CaptureError, ValueError):
        return None, "最新分隔与输入框之间仍有未识别的像素"


def draft_session_verdict(current, question, baseline):
    """A taller composer may crop an already verified empty tail. Compare the two original viewports."""
    kind, fault = _session_verdict(current, question)
    if kind:
        return kind, None
    anchors = role_nodes(current, 'session_divider')
    if not anchors:
        return None, fault
    if isinstance(baseline, Path):
        try:
            baseline = load_observation(baseline)
        except (OSError, ValueError):
            return None, '需要本题已确认的新会话原图'
    if (not baseline or session_evidence(baseline) != 'blank' or not chat_state(current) or dialog_target(current)
            or (current.package, current.activity, current.logical_size, current.physical_size) !=
               (baseline.package, baseline.activity, baseline.logical_size, baseline.physical_size)):
        return None, '需要同一小程序和尺寸下本题的空白新会话证据'
    source = session_boundary(baseline)['marker']
    bounds = {tuple(n['bounds']) for n in anchors if exact(n.get('text') or n.get('description') or '') == '已开始新会话'}
    if len(bounds) != 1:
        return None, '需要绑定原图可见的新会话分隔本身'
    box = bounds.pop()
    if any(abs(a-b) > current.logical_size[0]*.012 for a, b in zip(box, source)):
        return None, '当前分隔与新会话原图不能对齐'
    try:
        view, prior = answer_viewport(current), answer_viewport(baseline)
        marker = map_bounds(box, current.logical_size, current.physical_size)
        editor = chat_state(current)['input']
        if (not editor or editor.get('text') != question or prior[:3] != view[:3] or not view[3] < prior[3]
                or not _contains(view, marker) or view[3] - marker[3] < (marker[3]-marker[1]) * .5):
            return None, '需要发送前原题、完整分隔和可见的分隔下沿'
        # Recheck the entire shared viewport, plus even small changes below the divider.
        # The remaining, covered portion is accounted for by the prior blank tail.
        tail = (view[0], marker[3], view[2], view[3])
        if tail_rows(_labels(current), map_bounds(tail, current.physical_size, current.logical_size)):
            return None, '分隔下方仍有可见内容'
        after = np.asarray(_image(current.png).crop(tail), dtype=np.int16)
        if (not stable(baseline.png, current.png, bounds=view) or not stable(baseline.png, current.png, bounds=tail)
                or after.min() < 230 or np.ptp(after) > 14):
            return None, '发送前的可见对话或分隔下方像素与本题原图不一致'
    except (CaptureError, DeviceError):
        return None, '缺少可复验的完整对话视口'
    return 'aligned_divider', None


def session_evidence(observation, question=""):
    return _session_verdict(observation, question)[0]


def session_missing(observation, question=""):
    _, fault = _session_verdict(observation, question)
    return [fault] if fault else []


def clean_session(observation, question=""):
    return session_evidence(observation, question) is not None


def _new_session_defaults(observation):
    chat = chat_state(observation)
    if not chat:
        return []
    w, h = observation.logical_size
    labels = [v for v in _labels(observation) if v["label"] in {"开启新会话", "新会话"} and v["bounds"][3] < h * .14]
    if len(labels) == 1:
        return [_target(labels[0], observation.logical_size)]
    close, brand = chat["close"]["bounds"], chat["brand"]["bounds"]
    button_width = close[2] - close[0]
    point = (close[0] - button_width * 3.125, (close[1] + close[3]) / 2)
    if not max(brand[2], w * .48) < point[0] < w * .69:
        return []
    candidate = _target(chat["brand"], observation.logical_size, point)
    return [candidate] if candidate else []


def _set_stage(engine, task, stage):
    task.setdefault("direct_open", {})["stage"] = stage
    engine.save()


def _require_chat(engine):
    current = engine.observe()
    if not chat_state(current):
        raise DeviceError("小荷小程序页面或输入控件发生变化，未继续操作")
    return current


def _find_empty_tail(engine, task):
    step = ID + ".session_boundary"
    check = lambda current: clean_session(current, task["text"])
    if engine.goal_pending(step):
        return engine.perform(step, "确认最新分隔以下仅有空白或系统欢迎卡、没有用户提问", PACKAGE, check)
    previous = None
    for _ in range(10):
        current = _require_chat(engine)
        boundary = session_boundary(current)
        if check(current):
            if previous and previous[0].logical_size == current.logical_size and previous[1]["tail"] == boundary["tail"] and stable(
                    previous[0].png, current.png, bounds=boundary["physical_tail"]):
                return current
            previous = (current, boundary)
            time.sleep(.25)
            continue
        previous = None
        if boundary or dialog_target(current):
            break
        count = task["direct_open"].get("boundary_scrolls", 0)
        if count >= 8:
            break
        # Only the confirmed empty-session phase may seek downward through old
        # history.  No session button or question is replayed during this loop.
        viewport = answer_viewport(current)
        logical = map_bounds(viewport, current.physical_size, current.logical_size)
        width, height = current.logical_size
        task["direct_open"]["boundary_scrolls"] = count + 1
        engine.save()
        engine.device.swipe((width / 2, logical[1] + (logical[3] - logical[1]) * .8),
                            (width / 2, logical[1] + (logical[3] - logical[1]) * .3), .8, expected_package=PACKAGE)
        engine.log("douyin_direct_boundary_scrolled", ordinal=count + 1)
        time.sleep(.35)
    return engine.perform(step, "确认最新分隔以下仅有空白或系统欢迎卡、没有用户提问", PACKAGE, check,
                          hints=SESSION_HINTS)


def _drawer_closed(observation):
    return drawer_state(observation) is None and bool(chat_state(observation) or home_target(observation))


def reuse_foreground(engine):
    """Avoid sending a launcher intent that resets an already-open Xiaohe mini-app."""
    current = engine.observe()
    return current.package == PACKAGE and bool(chat_state(current) or home_target(current))


def open(engine, task):
    progress = task.setdefault("direct_open", {"stage": "discover"})
    if progress["stage"] == "discover":
        current = engine.observe()
        if chat_state(current) or home_target(current):
            _set_stage(engine, task, "home")
        else:
            defaults = label_taps(["搜索"])
            for close in [*label_taps(["关闭", "close"], [.75, 0, 1, .16]), {"actions": [{"type": "back"}]}]:
                defaults.extend({"actions": [*close["actions"], *search["actions"]]} for search in label_taps(["搜索"]))
            engine.perform(ID + ".open_search", "打开抖音搜索输入页，以名称查找小荷小程序", PACKAGE,
                           lambda o: _host(o) and edit(o, "douyin") is not None,
                           defaults=defaults, hints=["搜索"])
            _set_stage(engine, task, "search_input")
    if progress["stage"] == "search_input":
        engine.perform(ID + ".focus_search", "聚焦唯一的抖音搜索输入框", PACKAGE,
                       lambda o: edit(o, "douyin", focused=True) is not None, defaults=douyin.focus_defaults(), hints=["搜索"])
        field = edit(engine.observe(), "douyin", focused=True)
        if field is None:
            raise DeviceError("名称搜索输入框不唯一，未输入")
        if field.get("text") != QUERY:
            engine.device.input_text(QUERY, expected_package=PACKAGE, clear=True)
        current = engine.observe()
        field = edit(current, "douyin", focused=True)
        if field is None or field.get("text") != QUERY:
            raise DeviceError("名称搜索未能完整回读，未搜索")
        _set_stage(engine, task, "search_submit")
    if progress["stage"] == "search_submit":
        def before_search():
            field = edit(engine.observe(), "douyin", focused=True)
            if field is None or field.get("text") != QUERY:
                raise DeviceError("名称搜索前输入内容变化，未提交")
            progress["search_dispatched"] = True
            engine.save()
        engine.perform(ID + ".name_search", "单次搜索固定小程序名称，并确认首屏结果属于这个名称", PACKAGE,
                       lambda o: douyin.search_results_ready(o, QUERY), defaults=douyin.send_defaults(), risk="send",
                       force=not progress.get("search_dispatched"), before_action=before_search, hints=["搜索"])
        _set_stage(engine, task, "entry")
    if progress["stage"] == "entry":
        current = engine.observe()
        deadline = time.monotonic() + douyin.RESULT_READY_TIMEOUT
        while (douyin.search_results_ready(current, QUERY)
               and not douyin.results_loaded(current, QUERY)
               and not (named_entry_defaults(current) or _named_card_evidence(current))
               and time.monotonic() < deadline):
            time.sleep(.5)
            current = engine.observe()
        def before_entry():
            fresh = engine.observe()
            if not _named_card_evidence(fresh):
                raise DeviceError("未确认当前名称搜索结果中同卡片的小荷品牌和小程序标记")
            save_observation(engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "discovery", fresh)
        engine.perform(ID + ".open_miniapp", "进入同一卡片中唯一的小荷AI医生小程序，确认其首页或对话页；首页出现继续咨询AI医生也表示已进入", PACKAGE,
                       lambda o: bool(chat_state(o) or home_target(o)), defaults=named_entry_defaults(current),
                       hints=["小荷AI医生", "小程序", "进入"], risk="toggle", before_action=before_entry)
        _set_stage(engine, task, "home")
    if progress["stage"] == "home":
        current = engine.observe()
        home = home_target(current)
        engine.perform(ID + ".open_chat", "从小荷首页的健康问题输入入口或继续咨询AI医生进入对话输入页；此步不发送问题", PACKAGE, lambda o: chat_state(o) is not None,
                       defaults=[_target(home, current.logical_size)] if home else [], hints=["健康问题，随时问我", "继续咨询AI医生"], risk="toggle")
        _set_stage(engine, task, "session_request")
    if progress["stage"] == "session_request":
        current = engine.observe()
        close_step = ID + ".close_products"
        if drawer_state(current) is not None or engine.goal_pending(close_step):
            # Android back may dismiss the entire consultation together with its drawer.
            # Resolve that old dispatch against the branded landing page before navigating.
            current = engine.perform(close_step, "关闭上题药品列表，核对小荷首页或对话页", PACKAGE,
                                     _drawer_closed, defaults=[{"actions": [{"type": "back"}]}])
        home = home_target(current)
        if home is not None or engine.goal_pending(ID + ".return_to_chat"):
            current = engine.perform(ID + ".return_to_chat", "从已核实的小荷首页返回对话输入页", PACKAGE,
                                     lambda o: chat_state(o) is not None and drawer_state(o) is None,
                                     defaults=[_target(home, current.logical_size)] if home else [], risk="toggle",
                                     hints=["健康问题，随时问我", "继续咨询AI医生"])
        def before_new_session():
            scene = _require_chat(engine)
            save_observation(engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "before-reset", scene)
        engine.perform(ID + ".new_session", "打开新会话确认弹窗，并核对结束当前咨询的提示", PACKAGE,
                       lambda o: dialog_target(o) is not None, defaults=_new_session_defaults(current),
                       hints=["开启新会话", "新会话"], risk="toggle", before_action=before_new_session)
        _set_stage(engine, task, "session_confirm")
    if progress["stage"] == "session_confirm":
        current = engine.observe()
        target = dialog_target(current)
        dispatched_here = False
        def before_confirm():
            nonlocal dispatched_here
            if dialog_target(engine.observe()) is None:
                raise DeviceError("新会话确认弹窗已变化，未点击确定")
            dispatched_here = True
            progress["confirmation_dispatched"] = True
            engine.save()
        confirmed = engine.perform(ID + ".confirm_session", "确认开启新会话；只点击一次，不重复确认", PACKAGE,
                       lambda o: progress.get("confirmation_dispatched") is True and dialog_target(o) is None and chat_state(o) is not None,
                       defaults=[_target(target, current.logical_size)] if target else [], hints=["确定"],
                       risk="toggle", before_action=before_confirm)
        origin = engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "before-reset"
        if dispatched_here and (origin / "observation.json").is_file():
            signature = reset_welcome_witness(load_observation(origin), confirmed)
            if signature:
                save_observation(origin.parent / "after-reset", confirmed)
                progress['reset_welcome'] = signature
        _set_stage(engine, task, "session_boundary")
    if progress["stage"] == "session_boundary":
        current = _find_empty_tail(engine, task)
        boundary = session_boundary(current)
        save_observation(engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "new-session", current)
        kind = session_evidence(current, task["text"])
        progress.update(session_confirmed=True, boundary=boundary["marker"], session_evidence=kind)
        if kind == "welcome":
            origin = "candidate" if "perception.session_welcome" in engine.state.get("flow_candidates", {}) else "saved"
            engine.log("session_welcome_verified", source=origin)
        _set_stage(engine, task, "text_mode")
    if progress["stage"] == "text_mode":
        current = _require_chat(engine)
        chat = chat_state(current)
        voice = chat["voice"]
        defaults = label_taps(["切换文字输入"])
        if voice:
            box = voice["bounds"]
            point = (current.logical_size[0] * .115, (box[1] + box[3]) / 2)
            candidate = _target(voice, current.logical_size, point)
            if candidate:
                defaults.append(candidate)
        engine.perform(ID + ".text_mode", "切换为唯一的文字输入框", PACKAGE,
                       lambda o: bool(chat_state(o) and chat_state(o)["input"]), defaults=defaults,
                       hints=["按住说话", "切换文字输入"], risk="toggle")
        _set_stage(engine, task, "ready")
    current = _require_chat(engine)
    field = chat_state(current)["input"]
    if not progress.get("session_confirmed") or field is None or not (_input_empty(field) or field.get("text") == task["text"]):
        raise DeviceError("新会话或输入草稿无法确认，未输入问题")


def focus_defaults():
    return [tap("class_name", "android.widget.EditText", [0, .25, 1, .96])]


def send_defaults():
    return label_taps(["发送"], [.6, .25, 1, .96])


def _input_panel(observation, field):
    w, h = observation.logical_size
    candidates = [node["bounds"] for node in _visible(observation)
                  if re.search(r"ViewGroup|FrameLayout|LinearLayout", node.get("class_name", ""))
                  and _contains(node["bounds"], field["bounds"]) and node["bounds"][2] - node["bounds"][0] >= w * .65
                  and field["bounds"][3] - field["bounds"][1] < node["bounds"][3] - node["bounds"][1] < h * .35]
    return min(candidates, key=lambda b: b[3] - b[1], default=None)


def _up_arrow(observation, panel):
    """Recognize the upward send glyph inside the actual composer only."""
    pw, _ = observation.physical_size
    w, _ = observation.logical_size
    logical = (max(panel[0], panel[2] - w * .15), panel[1], panel[2], panel[3])
    left, top, right, bottom = map_bounds(logical, observation.logical_size, observation.physical_size)
    rgb = np.asarray(_image(observation.png), dtype=np.float32)
    gray = rgb[..., 0] * .2126 + rgb[..., 1] * .7152 + rgb[..., 2] * .0722
    center_x = (left + right) / 2
    best = None
    for ratio in (.075, .09, .105, .12):
        size = max(12, round(pw * ratio))
        for center_y in range(round(top + (bottom - top) * .48), round(bottom - size / 2) + 1, max(2, round(size * .06))):
            def sample(x, y, radius=.045):
                cx, cy = center_x + x * size, center_y - y * size
                r = max(1, round(size * radius))
                area = gray[max(top, round(cy - r)):min(bottom, round(cy + r) + 1),
                            max(left, round(cx - r)):min(right, round(cx + r) + 1)]
                return float((area < 120).mean()) if area.size else 0
            ring = [sample(np.cos(a * np.pi / 6) * .42, np.sin(a * np.pi / 6) * .42, .025) for a in range(12)]
            shaft = sum(sample(0, y) for y in (-.2, -.08, .03)) / 3
            head = sum(sample(x, y) for x, y in ((-.13, .08), (-.07, .14), (0, .2), (.07, .14), (.13, .08))) / 5
            sides = 1 - (sample(-.28, -.02, .07) + sample(.28, -.02, .07)) / 2
            clear_top = 1 - (sample(-.2, -.28, .06) + sample(.2, -.28, .06)) / 2
            score = None
            if shaft >= .18 and head >= .12 and sides >= .82 and clear_top >= .82 and max(ring) <= .12:
                score = shaft * .42 + head * .42 + sides * .1 + clear_top * .06
            if 1 - shaft >= .52 and 1 - head >= .38 and 1 - sides >= .58 and 1 - clear_top >= .58 and min(ring) >= .88:
                score = (1 - shaft) * .42 + (1 - head) * .42 + (1 - sides) * .1 + (1 - clear_top) * .06
            if score is not None and score >= .26 and (best is None or score > best[0]):
                box = (max(left, round(center_x - size / 2)), max(top, round(center_y - size / 2)),
                       min(right, round(center_x + size / 2)), min(bottom, round(center_y + size / 2)))
                best = (score, box)
    return map_bounds(best[1], observation.physical_size, observation.logical_size) if best else None


def send_target(observation):
    chat = chat_state(observation)
    field = chat["input"] if chat else None
    if field is None or not field.get("focused") or _input_empty(field):
        return None
    panel = _input_panel(observation, field)
    if panel is None:
        return None
    w, _ = observation.logical_size
    buttons = [item for item in _labels(observation) if item["label"] == "发送" and "node" in item
               and item["node"].get("clickable") and item["bounds"][0] >= w * .6 and _contains(panel, item["bounds"])]
    if len(buttons) > 1:
        return None
    box = buttons[0]["bounds"] if buttons else _up_arrow(observation, panel)
    return {"bounds": box, "panel": panel, "input": field["bounds"], "method": "label" if buttons else "up_arrow_pixels"} if box else None


def sent(observation, task):
    chat = chat_state(observation)
    if not chat or not _input_empty(chat["input"]):
        return False
    try:
        bubble = direct_question_bubble(observation, task["text"], answer_viewport(observation))
        if not bubble:
            return False
        markers = [item for item in _labels(observation) if item["label"] == "已开始新会话"]
        if not markers:
            return submission_evidence(task) is not None
        last = max(markers, key=lambda item: item["bounds"][3])
        marker_bottom = last["bounds"][3] * observation.physical_size[1] / observation.logical_size[1]
        if bubble["bounds"][1] <= marker_bottom:
            return False
        return bool(task.get("direct_open", {}).get("session_confirmed"))
    except (CaptureError, DeviceError):
        return False


def _evidence_hash(directory):
    digest = hashlib.sha256()
    for name in ("screen.png", "screen.xml", "observation.json"):
        digest.update(name.encode())
        digest.update((Path(directory) / name).read_bytes())
    return digest.hexdigest()


def submission_evidence(task, run_dir=None):
    """Read the original before/after evidence, never accept a bare flag."""
    proof = task.get("direct_submission", {})
    identity = [task.get("text"), PACKAGE, ID, task.get("index")]
    if (task.get("entry") != ID or proof.get("identity") != identity
            or proof.get("latest_user_question") is not True
            or proof.get("send_intent", {}).get("step") != ID + ".send"
            or proof.get("send_intent", {}).get("risk") != "send"
            or not isinstance(proof.get("send_intent", {}).get("before"), (int, float))):
        return None
    try:
        root = Path(proof["root"]).resolve()
        if run_dir is not None and root != Path(run_dir).resolve() / "调试产物" / ID / f"{task['index']:03d}":
            return None
        paths = {key: Path(proof[key]).resolve() for key in ("new_session", "before_send", "latest_tail", "question_observation")}
        ends, trace = proof.get("end_observations", []), proof.get("backtrack", [])
        if len(ends) != 3 or not 1 <= len(trace) <= 33:
            return None
        all_paths = set(paths.values()) | {Path(path).resolve() for path in ends} | {Path(row["observation"]).resolve() for row in trace}
        if (any(not path.is_relative_to(root) for path in paths.values())
                or paths["new_session"] != root / "new-session" or paths["before_send"] != root / "before-send"):
            return None
        if task.get('direct_open', {}).get('reset_welcome'):
            all_paths.update({root / 'before-reset', root / 'after-reset'})
        if any(not path.is_relative_to(root) or proof.get("evidence_hashes", {}).get(str(path)) != _evidence_hash(path) for path in all_paths):
            return None
        loaded = {path: load_observation(path) for path in all_paths}
        observations = {key: loaded[path] for key, path in paths.items()}
        before, new = observations["before_send"], observations["new_session"]
        if new.reset_welcome or before.reset_welcome:
            witness = reset_welcome_witness(loaded[root / 'before-reset'], loaded[root / 'after-reset'])
            if not witness or not (new.reset_welcome == before.reset_welcome == witness):
                return None
        before_chat = chat_state(before)
        new_kind = session_evidence(new)
        draft_kind, _ = draft_session_verdict(before, task["text"], new)
        if (not before_chat or not before_chat["input"] or before_chat["input"].get("text") != task["text"]
                or not draft_kind or proof.get("draft_evidence", draft_kind) != draft_kind or not new_kind
                or proof.get("session_evidence", new_kind) != new_kind):
            return None
        geometry = tuple(before.logical_size), tuple(before.physical_size)
        for observation in loaded.values():
            if (tuple(observation.logical_size), tuple(observation.physical_size)) != geometry or not chat_state(observation):
                return None
        tail, question = observations["latest_tail"], observations["question_observation"]
        viewport = answer_viewport(tail)
        if (not _input_empty(chat_state(tail)["input"]) or not _input_empty(chat_state(question)["input"])
                or not direct_question_bubble(question, task["text"], answer_viewport(question))):
            return None
        end_frames = [loaded[Path(path).resolve()] for path in ends]
        if (any(not _input_empty(chat_state(frame)["input"]) or answer_viewport(frame) != viewport
                or any(re.search(r"停止生成|正在思考|生成中|正在生成|加载中|加载失败|重新加载|网络不稳定", item["label"]) for item in _labels(frame))
                for frame in end_frames)
                or not _answer_content_visible(tail, viewport, task["text"])
                or any(not stable(a.png, b.png, bounds=viewport) for a, b in zip(end_frames, end_frames[1:]))
                or not stable(end_frames[-1].png, tail.png, bounds=viewport)):
            return None
        trace_frames = [loaded[Path(row["observation"]).resolve()] for row in trace]
        if not stable(trace_frames[0].png, tail.png, bounds=viewport) or not stable(trace_frames[-1].png, question.png, bounds=viewport):
            return None
        for index, frame in enumerate(trace_frames):
            if not _input_empty(chat_state(frame)["input"]) or answer_viewport(frame) != viewport:
                return None
            candidate = latest_user_bubble(frame, viewport)
            target = direct_question_bubble(frame, task["text"], viewport)
            if target and index != len(trace_frames) - 1:
                return None  # A complete nearest question must end the scan.
            if index:
                direction = trace[index].get("seam", {}).get("direction")
                prior = latest_user_bubble(trace_frames[index - 1], viewport)
                expected = "down" if prior and prior["clipped_bottom"] else "up"
                if direction != expected:
                    return None
                measured = direct_scroll_seam(trace_frames[index - 1], frame, viewport, direction)
                if abs(measured["shift"] - trace[index]["seam"].get("shift", -100)) > 2:
                    return None
            if candidate and candidate["complete"] and not target:
                return None
        return proof
    except (OSError, ValueError, KeyError, TypeError, CaptureError, DeviceError):
        return None


def capture_identity(engine, question):
    task = next((item for item in engine.state.get("tasks", []) if item.get("entry") == ID
                 and item.get("index") == engine.state.get("index") and item.get("text") == question), None)
    return task is not None and submission_evidence(task, engine.run_dir) is not None


class _SubmissionProbe(AnswerCapture):
    """Reuse capture's read/scroll checks without its navigation recoveries."""

    def _observe(self, ocr=False):
        current = self.engine.decorate(self.device.observe(ocr=True))
        self._last = current
        chat = chat_state(current)
        if (not chat or not _input_empty(chat["input"]) or dialog_target(current) is not None
                or drawer_state(current) is not None or guide_visible(current)
                or (tuple(current.logical_size), tuple(current.physical_size)) != self.geometry
                or tuple(answer_viewport(current)) != tuple(self.viewport)):
            raise CaptureError("发送确认的页面、输入框或视口变化，保留现场且不操作")
        return current

    def _swipe(self, direction, fraction=.43, alternate=False, *, duration=.4):
        # These are observation-only body scrolls.  Each gesture rechecks the
        # shell and empty input; no tap, back, new session or retry is available.
        self._observe()
        if self.scrolls >= 32:
            raise CaptureError("发送确认已达到32次只读滚动上限，不能进入更早会话")
        self.scrolls += 1
        self.engine.log("submission_read_scroll", direction=direction, count=self.scrolls)
        return super()._swipe(direction, fraction, alternate, duration=duration)

    def _dismiss_guide(self, observation, pending=False):
        if guide_visible(observation):
            raise CaptureError("发送确认遇到引导遮挡，仅保留现场，不点击关闭")
        return observation

    def _recover_network(self, observation):
        raise CaptureError("发送确认遇到网络异常，仅保留现场，不点击重试")

    def _search_source(self, question):
        return None

    def _direct_identity_verified(self, question):
        # The factory below has just read the original empty-tail/exact-draft
        # evidence and the durable inflight send. No persisted shortcut exists.
        return self.intent_verified and self.question == question


def confirm_submission(engine, task, observation=None):
    """Recover the original question after automatic answer scrolling.

    Existing send/session evidence authorizes only reading the latest session.
    Start at the real bottom, then stop at its first user question: an older
    identical question cannot satisfy this recovery.  The caller retains the inflight
    checkpoint until the original ``sent`` postcondition actually passes.
    """
    progress, inflight = task.get("direct_open", {}), engine.state.get("inflight", {})
    if (task.get("phase") != "submitting" or not progress.get("session_confirmed")
            or inflight.get("step") != ID + ".send" or inflight.get("risk") != "send"
            or (engine.state.get("entry"), engine.state.get("index")) != (ID, task["index"])):
        raise CaptureError("缺少当前原题的单次发送断点，不能执行发送恢复滚动")
    root = engine.run_dir / "调试产物" / ID / f"{task['index']:03d}"
    try:
        before, new_session = load_observation(root / "before-send"), load_observation(root / "new-session")
    except (OSError, ValueError, KeyError) as error:
        raise CaptureError("缺少发送前原题或新会话原始证据，保留发送断点") from error
    before_chat = chat_state(before)
    draft_kind, _ = draft_session_verdict(before, task["text"], new_session)
    if (not before_chat or not before_chat["input"] or before_chat["input"].get("text") != task["text"]
            or not draft_kind or not clean_session(new_session)
            or (before.logical_size, before.physical_size) != (new_session.logical_size, new_session.physical_size)):
        raise CaptureError("发送前原题或新会话证据不匹配，不能扫描历史回答")
    probe = _SubmissionProbe(engine.device, engine)
    probe.intent_verified = True
    probe.package, probe.question, probe.counter, probe.scrolls = PACKAGE, task["text"], 0, 0
    probe.geometry = tuple(before.logical_size), tuple(before.physical_size)
    current = observation if observation is not None else engine.observe()
    probe.viewport = answer_viewport(current)
    probe.attempt = root / ("submission-confirm-" + uuid4().hex[:12])
    probe.attempt.mkdir(parents=True, exist_ok=True)
    current = probe._observe()
    probe._save("initial", current)
    current = probe._complete(current, task["text"])
    current, target = probe._top(current, task["text"])
    if not _input_empty(chat_state(current)["input"]) or not target:
        raise CaptureError("只读滚动后仍未同时确认最新用户原题与空输入框")
    evidence = probe._save("question-confirmed", current)
    task["direct_submission"] = {
        "identity": [task["text"], PACKAGE, ID, task["index"]], "root": str(root.resolve()),
        "new_session": str((root / "new-session").resolve()), "before_send": str((root / "before-send").resolve()),
        "latest_tail": probe._latest_tail_evidence, "question_observation": evidence,
        "send_intent": dict(inflight), "latest_user_question": True, "session_evidence": session_evidence(new_session), "draft_evidence": draft_kind,
        "end_observations": probe._direct_end_observations, "backtrack": probe._direct_backtrack,
    }
    proof = task["direct_submission"]
    paths = {proof[key] for key in ("new_session", "before_send", "latest_tail", "question_observation")}
    if progress.get('reset_welcome'):
        paths.update({root / 'before-reset', root / 'after-reset'})
    paths.update(proof["end_observations"])
    paths.update(row["observation"] for row in proof["backtrack"])
    proof["evidence_hashes"] = {str(Path(path).resolve()): _evidence_hash(path) for path in paths}
    engine.save()
    engine.log("submission_confirmed_by_reading", scrolls=probe.scrolls, evidence=evidence,
               exact_question=True, newest_session=True, input_empty=True, replayed=False)
    return current


def _prepare_draft(engine, task):
    current = _require_chat(engine)
    field = chat_state(current)["input"]
    if not task.get("direct_open", {}).get("session_confirmed") or field is None:
        raise DeviceError("没有已确认的新会话，未输入")
    if not (_input_empty(field) or field.get("text") == task["text"]):
        raise DeviceError("输入框存在其他草稿，未覆盖")
    engine.perform(ID + ".focus_input", "聚焦小荷小程序唯一的问题输入框", PACKAGE,
                   lambda o: bool(chat_state(o) and chat_state(o)["input"] and chat_state(o)["input"].get("focused")),
                   defaults=focus_defaults(), hints=INPUT_HINTS)
    current = _require_chat(engine)
    field = chat_state(current)["input"]
    if not field or not field.get("focused") or not (_input_empty(field) or field.get("text") == task["text"]):
        raise DeviceError("输入前焦点或草稿变化，未写入")
    if field.get("text") != task["text"]:
        engine.device.input_text(task["text"], expected_package=PACKAGE, clear=True)


def _verified_draft(engine, task):
    first = _require_chat(engine)
    field = chat_state(first)["input"]
    if not field or not field.get("focused") or field.get("text") != task["text"]:
        raise DeviceError("输入后无法精确回读原题，未发送")
    first_target = send_target(first)
    time.sleep(.25)
    current = _require_chat(engine)
    field, target = chat_state(current)["input"], send_target(current)
    if (not target or not first_target or field.get("text") != task["text"]
            or current.logical_size != first.logical_size or current.physical_size != first.physical_size
            or any(tuple(target[key]) != tuple(first_target[key]) for key in ("bounds", "panel", "input"))
            or not stable(first.png, current.png, bounds=map_bounds(target["bounds"], current.logical_size, current.physical_size))):
        # No inferred send is performed.  Resuming this phase can re-read a
        # stable button; it does not overwrite any unrelated draft.
        engine._request(ID + ".send_control", "确认当前原题的唯一发送按钮；不能点击或重复发送", current, SEND_HINTS,
                        "发送按钮或输入布局尚未可靠识别", uncertain_send=True)
    baseline = engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "new-session"
    if (draft_session_verdict(first, task["text"], baseline)[0]
            and draft_session_verdict(current, task["text"], baseline)[0]):
        engine.resolve_capture_request(BEFORE_SEND)
        return current, target
    # A missing witness before dispatch is a read-only review of the existing draft.
    recipe, observation, file = engine._request(BEFORE_SEND, "发送前核实本题草稿与当前新会话", current,
                                               SESSION_HINTS, "不能证明分隔以下只有空白或系统欢迎卡，尚未发送")
    engine._accept_perception(recipe, observation, file)
    return None


def submit(engine, task):
    from ..workflow import _phase, _uncertain
    if not engine.goal_pending(BEFORE_SEND):
        _prepare_draft(engine, task)
    while not (ready := _verified_draft(engine, task)):
        pass  # Each accepted perception response requires a fresh read and the same checks.
    current, target = ready
    save_observation(engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / "before-send", current)
    engine.log("question_input_verified", characters=len(task["text"]), entry_id=ID, send_method=target["method"])
    engine.resolve_capture_request(ID + ".send_control")
    _phase(engine, task, "submitting")
    engine.state["inflight"] = {"step": ID + ".send", "risk": "send", "before": time.time()}
    engine.save()
    box = target["bounds"]
    engine.device.tap(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), expected_package=PACKAGE)
    engine.log("douyin_direct_question_sent", submission_count=1)
    deadline = time.monotonic() + 12
    while True:
        current = engine.observe()
        chat = chat_state(current)
        if chat and _input_empty(chat["input"]):
            try:
                current = confirm_submission(engine, task, current)
            except (CaptureError, DeviceError) as error:
                _uncertain(engine, task, engine.observe(), "已单次发送；只读核实未通过，不重发：" + str(error))
            if sent(current, task):
                engine.state.pop("inflight", None)
                _phase(engine, task, "submitted")
                return
            _uncertain(engine, task, current, "已单次发送，尚不能同时确认本题气泡和输入框清空；不重发")
        if time.monotonic() >= deadline:
            _uncertain(engine, task, current, "已单次发送，输入框尚未清空或对话页变化；不重发")
        time.sleep(.25)


def can_enter_current(observation, task):
    return False


def enter_answer(engine, task, evidence_dir=None):
    return None


def answer_ready(observation):
    return chat_state(observation) is not None and dialog_target(observation) is None


def goal_check(step, task):
    """Expose real postconditions to the compact Agent handoff protocol."""
    progress = task.get("direct_open", {})
    def current_question_top(observation):
        try:
            viewport = answer_viewport(observation)
            target = direct_question_bubble(observation, task["text"], viewport)
            return bool(submission_evidence(task) and sent(observation, task) and target
                        and (target["bounds"][1] - viewport[1]) / (viewport[3] - viewport[1]) <= .20)
        except (CaptureError, DeviceError):
            return False
    checks = {
        "capture.question_top": current_question_top,
        ID + ".open_search": lambda o: _host(o) and edit(o, "douyin") is not None,
        ID + ".focus_search": lambda o: edit(o, "douyin", focused=True) is not None,
        ID + ".name_search": lambda o: douyin.search_results_ready(o, QUERY),
        ID + ".open_miniapp": lambda o: bool(chat_state(o) or home_target(o)),
        ID + ".open_chat": lambda o: chat_state(o) is not None,
        ID + ".close_products": _drawer_closed,
        ID + ".return_to_chat": lambda o: chat_state(o) is not None and drawer_state(o) is None,
        ID + ".new_session": lambda o: dialog_target(o) is not None,
        ID + ".confirm_session": lambda o: progress.get("confirmation_dispatched") is True and dialog_target(o) is None and chat_state(o) is not None,
        ID + ".session_boundary": lambda o: clean_session(o, task["text"]),
        ID + ".text_mode": lambda o: bool(chat_state(o) and chat_state(o)["input"]),
        ID + ".focus_input": lambda o: bool(chat_state(o) and chat_state(o)["input"] and chat_state(o)["input"].get("focused")),
        BEFORE_SEND: lambda o: bool(clean_session(o, task["text"]) and send_target(o) and chat_state(o)["input"].get("text") == task["text"]),
        ID + ".send_control": lambda o: bool(chat_state(o) and chat_state(o)["input"] and chat_state(o)["input"].get("text") == task["text"] and send_target(o)),
        ID + ".send": lambda o: sent(o, task),
    }
    return checks.get(step)
