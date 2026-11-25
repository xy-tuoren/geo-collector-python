"""One-way answer capture; actions never submit or replace a question."""
from __future__ import annotations

from pathlib import Path
import re
import time
import unicodedata
from uuid import uuid4

import numpy as np
from PIL import Image

from .device import map_bounds, physical_to_logical, validate_geometry
from .images import CaptureError, _image, compose, compose_images, crop, image_size, match_overlap, match_product_overlap, stable
from .recovery import load_observation, save_observation
from .rules import normalized
from .storage import read_json, write_json
from .goals import role_nodes, role_labels
from .carousels import ProductCarousels, product_carousels, verify_carousels


NUMBER = r"[0-9零〇一二三四五六七八九十百两]+"
ITEM = NUMBER + r"(?:篇|份|条|部|本|项)(?:药品说明书?|说明书|医学文献|临床文献|文献|医学资料|临床指南|指南|专家共识)"
TITLE = r"^参考" + ITEM + r"(?:(?:以及|和|及|与|、|\+|&)" + ITEM + r")*$"
TITLE_HINT = r"^参考[0-9零〇一二三四五六七八九十百两]+(?:篇|份|条|部|本|项).*(?:文献|资料|指南|共识|说明书?|说明)[\s\^∧︿⌃˄∨⌄vVへ~～]*$"
SOURCE = re.compile(r"^(?:药监局|医学文献|药品说明书?|临床指南|指南|专家共识|期刊)$")
NON_MONOGRAPH_HEADER_ITEM = re.compile(
    r"(" + NUMBER + r")(?:篇|份|条|部|本|项)(?!药品说明书?|说明书)(?:医学文献|临床文献|文献|医学资料|临床指南|指南|专家共识)"
)
NUMBERED_CITATION_ROW = re.compile(r"^\s*(\d{1,2})\s*[.、．]\s*(.+)$")
MONOGRAPH_ROW_PATTERN = re.compile(r"(?:_|__|\s)?药品说明书?(?:\s|$)|药监局$")
MONOGRAPH_TAG_PATTERN = re.compile(r"^(?:药监局|药品说明书?)$")
CITATION_DETAIL_LOADING = re.compile(r"加载中|正在加载")
CITATION_DETAIL_ERROR = re.compile(r"加载失败|网络异常|网络错误|重新加载|页面无法访问|内容加载失败")
CITATION_DETAIL_CUES = re.compile(r"医学文献|临床文献|临床指南|专家共识|医学资料|发布期刊|发布时间|内容摘要|查看资料原文|文献详情|期刊|摘要|上传")
PRODUCT_TITLE = r"^(?:参考|推荐)药品(?:\s*[（(]?[0-9]+(?:种|个|款)[)）]?)?$"
PRODUCT_ENTRY = r"^(?:查看(?:全部|所有)(?:参考|推荐)?药品|全部药品|更多药品)\s*(?:[>›〉》»→⟩]\s*)?$"
GUIDE_HINT = r"^在这里查看[「『“\"]?历史对话[」』”\"]?$"


def guide_visible(observation):
    return any(normalized(_label(item)) == "在这里查看历史对话" for item in [*observation.nodes, *_ocr(observation)])


def _composer_bounds(observation):
    w, h = observation.logical_size
    nodes = [n for n in observation.nodes if n.get("package") in ("", observation.package)]
    editors = [n["bounds"] for n in nodes
               if (n.get("class_name", "").endswith("EditText") or re.match(r"^(?:输入问题|输入消息|发送消息|按住说话)", _label(n)))
               and 0 <= n["bounds"][0] < n["bounds"][2] <= w and h * .6 <= n["bounds"][1] < n["bounds"][3] <= h
               and n["bounds"][2] - n["bounds"][0] >= w * .4]
    containers = [n["bounds"] for n in nodes
                  if h * .6 <= n["bounds"][1] < n["bounds"][3] <= h
                  and n["bounds"][2] - n["bounds"][0] >= w * .4
                  and n["bounds"][3] - n["bounds"][1] <= h * .2]
    for line in _ocr(observation):
        if not re.match(r"^(?:输入问题|输入消息|发送消息|按住说话)", _label(line)):
            continue
        box = map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
        center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        editors.extend(parent for parent in containers
                       if parent[0] <= center[0] <= parent[2] and parent[1] <= center[1] <= parent[3])
    return list(dict.fromkeys(map(tuple, editors)))


def answer_page_ready(observation):
    """A sidebar's scroll container is not the answer's visible composer."""
    return bool(_composer_bounds(observation))


def _label(item):
    return str(item.get("text") or item.get("description") or "").strip()


def _exact_question(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))


def _ocr(observation):
    return [line for line in observation.texts if line.get("confidence", 0) >= .78]


def answer_viewport(observation):
    """Physical viewport from actual input/scroll bounds and fixed headers."""
    if guide_visible(observation):
        raise CaptureError("历史对话引导浮层遮挡底层层级，需先关闭引导再识别回答视口")
    lw, lh = observation.logical_size
    validate_geometry(observation.logical_size, observation.physical_size)
    nodes = [node for node in observation.nodes if node.get("package") in ("", observation.package)]
    scrolls = [node["bounds"] for node in nodes if node.get("scrollable")
               and node["bounds"][2] - node["bounds"][0] >= lw * .65
               and node["bounds"][3] - node["bounds"][1] >= lh * .35]
    scroll = max(scrolls, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]), default=None)
    inputs = _composer_bounds(observation)
    if not scroll and not inputs:
        raise CaptureError("无法从滚动区域或输入栏确定回答视口，拒绝猜测底部裁剪")
    top = lh * .08
    if scroll and scroll[1] < lh * .2:
        top = max(top, scroll[1])
    for node in nodes:
        if node["bounds"][3] < lh * .20 and re.fullmatch(r"小荷AI医生|关闭|close", _label(node), re.I):
            top = max(top, node["bounds"][3] + lh * .006)
    for line in _ocr(observation):
        if line["bounds"][3] < observation.physical_size[1] * .25 and "AI回答仅供参考" in _label(line):
            top = max(top, line["bounds"][3] * lh / observation.physical_size[1] + lh * .006)
    if (observation.package == "com.ss.android.article.news" and scroll
            and any(node.get("resource_id", "").endswith(":id/title") and _label(node) == "小荷AI医生"
                    and node["bounds"][3] < lh * .12 for node in nodes)
            and any(node.get("class_name", "").endswith("EditText") and node["bounds"][1] > lh * .70 for node in nodes)):
        webs = [node["bounds"] for node in nodes if node.get("class_name") == "android.webkit.WebView"
                and _label(node) == "小荷AI医生" and node["bounds"][2] - node["bounds"][0] >= lw * .90
                and node["bounds"][3] - node["bounds"][1] >= lh * .50]
        # The legacy Toutiao notice belongs directly below its WebView header;
        # matching prose farther inside the answer is not a crop boundary.
        for label in _physical_labels(observation) if webs else ():
            box = map_bounds(label["bounds"], observation.physical_size, observation.logical_size)
            if (re.match(r"^AI生成非医疗诊断", _label(label)) and box[3] < lh * .20
                    and box[3] - box[1] <= lh * .05
                    and any(web[0] <= box[0] < box[2] <= web[2] and 0 <= box[1] - web[1] <= lh * .04 for web in webs)):
                top = max(top, box[3] + lh * .006)
    bottom = min([lh * .96, *([scroll[3]] if scroll else []), *(box[1] - lh * .012 for box in inputs)])
    # Mini-app shortcut strips sit above the composer but outside the answer.
    # Require several known controls in one real, compact full-width container;
    # a mention of a control in answer prose cannot become a crop boundary.
    controls = [map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
                for line in _ocr(observation)
                if re.fullmatch(r"报告解读|立即下载|拍报告|拍患处|拍药盒|就医", _label(line))]
    controls += [node["bounds"] for node in nodes if re.fullmatch(r"报告解读|立即下载|拍报告|拍患处|拍药盒|就医", _label(node))]
    if inputs:
        input_top = min(box[1] for box in inputs)
        strips = []
        for node in nodes:
            box = node["bounds"]
            if (box[2] - box[0] < lw * .85 or not lh * .65 <= box[1] < box[3] <= input_top
                    or box[3] - box[1] > lh * .12 or input_top - box[3] > lh * .065):
                continue
            contained = {tuple(control) for control in controls
                         if box[0] <= control[0] < control[2] <= box[2]
                         and box[1] <= control[1] < control[3] <= box[3]}
            if len(contained) >= 2:
                strips.append(box)
        if strips:
            bottom = min(bottom, min(box[1] for box in strips))
    content_boxes = [node["bounds"] for node in nodes
                     if node["bounds"][2] - node["bounds"][0] >= lw * .90
                     and lh * .07 <= node["bounds"][1] < lh * .20
                     and node["bounds"][3] - node["bounds"][1] >= lh * .35
                     and abs(node["bounds"][3] - bottom) <= lh * .008]
    if content_boxes:
        top = max(top, min(box[1] for box in content_boxes))
    patient_labels = [node["bounds"] for node in nodes if re.fullmatch(r"(?:不选|选择|切换)咨询人", _label(node))]
    patient_labels += [map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
                       for line in _ocr(observation) if re.fullmatch(r"(?:不选|选择|切换)咨询人", _label(line))]
    for node in nodes:
        row = node["bounds"]
        if (row[2] - row[0] < lw * .90 or not lh * .04 <= row[1] < row[3] <= lh * .22
                or row[3] - row[1] > lh * .10):
            continue
        if any(row[0] <= label[0] < label[2] <= row[2] and row[1] <= label[1] < label[3] <= row[3] for label in patient_labels):
            # Short answers expose the same body container with scrollable=false.
            # Its adjacency to the fixed patient row is the boundary evidence.
            adjacent = [box for box in content_boxes if 0 <= box[1] - row[3] <= lh * .02]
            if adjacent:
                top = max(top, max(box[1] for box in adjacent))
    left, right = (max(0, scroll[0]), min(lw, scroll[2])) if scroll else (0, lw)
    if not 0 <= top < bottom <= lh or bottom - top < lh * .35:
        raise CaptureError("回答视口异常，已在滚动前停止")
    return map_bounds((left, top, right, bottom), observation.logical_size, observation.physical_size)


def session_boundary(observation, viewport):
    """Read the empty-session marker; it may turn into a time after sending."""
    if observation.package != "com.ss.android.ugc.aweme" or "miniapp" not in observation.activity.lower():
        return None
    width = observation.physical_size[0]
    markers = [line["bounds"] for line in _physical_labels(observation)
               if _exact_question(_label(line)) == "已开始新会话"
               and viewport[1] <= line["bounds"][1] < line["bounds"][3] <= viewport[3]
               and line["bounds"][2] - line["bounds"][0] < width * .5
               and abs((line["bounds"][0] + line["bounds"][2]) / 2 - width / 2) < width * .15]
    return max(markers, key=lambda box: box[3], default=None)


def question_bubble(observation, question, viewport):
    """Exact OCR inside a complete right-side coloured bubble, including wraps."""
    expected = _exact_question(question)
    if not expected:
        raise CaptureError("问题不能为空")
    boundary = session_boundary(observation, viewport)
    content_top = boundary[3] if boundary else viewport[1]
    latest_first = observation.package == "com.ss.android.ugc.aweme" and "miniapp" in observation.activity.lower()
    learned = sorted(role_nodes(observation, "question"), key=lambda node: node["bounds"][1], reverse=latest_first)
    for node in learned:
        box = map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
        if not viewport[0] <= box[0] < box[2] <= viewport[2] or not content_top < box[1] < box[3] < viewport[3]:
            continue
        if box[3] - box[1] > (viewport[3] - viewport[1]) * .55:
            continue
        # Even a learned role cannot turn a composer or an entire answer into a question.
        if any(n.get("class_name", "").endswith("EditText") and n["bounds"][0] < node["bounds"][2]
               and n["bounds"][2] > node["bounds"][0] and n["bounds"][1] < node["bounds"][3]
               and n["bounds"][3] > node["bounds"][1] for n in observation.nodes):
            continue
        lines = [line for line in _ocr(observation) if box[0] <= line["bounds"][0] < line["bounds"][2] <= box[2]
                 and box[1] <= line["bounds"][1] < line["bounds"][3] <= box[3]]
        lines.sort(key=lambda line: (line["bounds"][1], line["bounds"][0]))
        text = "".join(_label(line) for line in lines)
        if _exact_question(text) == expected:
            return {"bounds": tuple(map(int, box)), "text_bounds": tuple(map(int, box)), "method": "learned_question_role_and_exact_ocr"}
    rgb = np.asarray(_image(observation.png), dtype=np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (r <= 100) & (g >= 150) & (g <= 240) & (b >= 85) & (b <= 235) & (g - r >= 70) & (b - r >= 45) & (np.abs(g - b) <= 105)
    left, top, right, bottom = map(int, viewport)
    area = mask[top:bottom, left:right]
    width, height = right - left, bottom - top
    rows = (area.sum(axis=1) >= width * .12) & area[:, int(width * .88):].any(axis=1)
    edges = np.diff(np.r_[False, rows, False].astype(np.int8))
    candidates = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    for start, end in reversed(candidates) if latest_first else candidates:
        if start <= 1 or end >= height - 1 or end - start < observation.physical_size[1] * .014:
            continue
        yy, xx = np.nonzero(area[start:end])
        box = (int(xx.min()) + left, int(start + top), int(xx.max()) + left + 1, int(end + top))
        if box[1] <= content_top:
            continue
        if area[start:end, xx.min():xx.max() + 1].mean() < .58:
            continue
        lines = [line for line in _ocr(observation) if line["bounds"][0] >= box[0]
                 and line["bounds"][1] >= box[1] and line["bounds"][2] <= box[2] and line["bounds"][3] <= box[3]]
        lines.sort(key=lambda line: (line["bounds"][1], line["bounds"][0]))
        if _exact_question("".join(_label(line) for line in lines)) == expected:
            return {"bounds": box, "text_bounds": (min(line["bounds"][0] for line in lines), min(line["bounds"][1] for line in lines), max(line["bounds"][2] for line in lines), max(line["bounds"][3] for line in lines)), "method": "exact_ocr_and_coloured_bubble"}
    return None


def latest_user_bubble(observation, viewport):
    """Return the nearest visible user bubble, including a clipped/unread one.

    Unlike question_bubble this never skips a different question to find the
    requested text in older history.  Direct MiniApp recovery uses it while
    walking backwards from the independently verified latest answer end.
    """
    rgb = np.asarray(_image(observation.png), dtype=np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (r <= 100) & (g >= 150) & (g <= 240) & (b >= 85) & (b <= 235) & (g - r >= 70) & (b - r >= 45) & (np.abs(g - b) <= 105)
    left, top, right, bottom = map(int, viewport)
    area = mask[top:bottom, left:right]
    width, height = right - left, bottom - top
    rows = (area.sum(axis=1) >= width * .12) & area[:, int(width * .88):].any(axis=1)
    edges = np.diff(np.r_[False, rows, False].astype(np.int8))
    for start, end in reversed(list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))):
        yy, xx = np.nonzero(area[start:end])
        if not len(xx) or area[start:end, xx.min():xx.max() + 1].mean() < .58:
            continue
        # Keep edge fragments: otherwise an unread latest question could be
        # skipped and an older identical question accepted in its place.
        clipped = start <= 1 or end >= height - 1
        if not clipped and end - start < observation.physical_size[1] * .014:
            continue
        box = (int(xx.min()) + left, int(start + top), int(xx.max()) + left + 1, int(end + top))
        lines = sorted((line for line in _ocr(observation) if box[0] <= line["bounds"][0] < line["bounds"][2] <= box[2]
                        and box[1] <= line["bounds"][1] < line["bounds"][3] <= box[3]),
                       key=lambda line: (line["bounds"][1], line["bounds"][0]))
        text_box = (min(line["bounds"][0] for line in lines), min(line["bounds"][1] for line in lines),
                    max(line["bounds"][2] for line in lines), max(line["bounds"][3] for line in lines)) if lines else box
        return {"bounds": box, "text_bounds": text_box, "text": "".join(_label(line) for line in lines),
                "complete": not clipped, "clipped_top": bool(start <= 1), "clipped_bottom": bool(end >= height - 1),
                "method": "latest_user_bubble_after_verified_tail"}
    return None


def direct_question_bubble(observation, question, viewport):
    candidate = latest_user_bubble(observation, viewport)
    boundary = session_boundary(observation, viewport)
    if boundary and (not candidate or candidate["bounds"][1] <= boundary[3]):
        raise CaptureError("最新新会话分界之后没有本题气泡，不能继续进入旧会话")
    if not candidate or not candidate["complete"]:
        return None
    if _exact_question(candidate["text"]) != _exact_question(question):
        raise CaptureError("从最新回答末尾遇到的第一条用户问题不匹配或无法完整识别，不能跳过它查找旧同题")
    return candidate


def direct_scroll_seam(before, after, viewport, direction):
    """Verify read-scroll continuity on both sides of the floating center CTA.

    No image is altered for delivery. Independent left/right original pixels
    must agree on displacement and retain an overlap; the full screenshot is
    still used to detect the nearest question on every observation.
    """
    if stable(before.png, after.png, bounds=viewport):
        return {"verified": True, "unchanged": True, "shift": 0, "direction": direction}
    left, top, right, bottom = viewport
    width, height = right - left, bottom - top
    regions = [(left, top, left + round(width * .40), bottom),
               (left + round(width * .62), top, right, bottom)]
    pairs = (after, before) if direction == "up" else (before, after)
    seams = [match_overlap(crop(pairs[0].png, region), crop(pairs[1].png, region)) for region in regions]
    if (abs(seams[0]["shift"] - seams[1]["shift"]) > 2
            or min(seam["overlap"] for seam in seams) < height * .30
            or not 0 < max(seam["shift"] for seam in seams) <= height * .70):
        raise CaptureError("最新问题回溯的两侧位移或重叠不一致，不能继续查找旧同题")
    return {"verified": True, "unchanged": False, "direction": direction,
            "shift": max(seam["shift"] for seam in seams), "regions": [list(region) for region in regions], "seams": seams}


def _direct_scroll_button(observation, base):
    """Recognize the MiniApp's unlabelled, non-clickable white down button."""
    if observation.package != "com.ss.android.ugc.aweme" or "miniapp" not in observation.activity.lower():
        return None
    from .platforms.douyin_direct import chat_state
    if not chat_state(observation):
        return None
    lw, lh = observation.logical_size
    groups = {}
    for node in observation.nodes:
        if node.get("package") == observation.package and not _label(node) and not node.get("resource_id"):
            groups.setdefault(tuple(node["bounds"]), set()).add(node.get("class_name"))
    candidates = []
    for box, classes in groups.items():
        if not {"android.widget.ScrollView", "android.widget.HorizontalScrollView", "android.widget.LinearLayout"}.issubset(classes):
            continue
        width, height = box[2] - box[0], box[3] - box[1]
        if not (lw * .075 <= width <= lw * .16 and .9 <= width / max(height, 1) <= 1.1
                and lw * .45 <= (box[0] + box[2]) / 2 <= lw * .55 and lh * .65 <= box[1] < box[3] <= lh * .92):
            continue
        physical = map_bounds(box, observation.logical_size, observation.physical_size)
        if not base[1] < physical[1] < physical[3] <= base[3] or base[3] - physical[3] > observation.physical_size[1] * .12:
            continue
        icons = [b for b, c in groups.items() if "android.widget.ImageView" in c
                 and box[0] < b[0] < b[2] < box[2] and box[1] < b[1] < b[3] < box[3]
                 and .30 <= (b[2] - b[0]) / width <= .60 and .30 <= (b[3] - b[1]) / height <= .60
                 and abs(sum(b[::2]) - sum(box[::2])) <= width * .12
                 and abs(sum(b[1::2]) - sum(box[1::2])) <= height * .12]
        if len(icons) != 1:
            continue
        pixels = np.asarray(_image(crop(observation.png, physical)))
        yy, xx = np.indices(pixels.shape[:2])
        radius = np.sqrt(((xx + .5) / pixels.shape[1] - .5) ** 2 + ((yy + .5) / pixels.shape[0] - .5) ** 2)
        ring = (radius > .31) & (radius < .44)
        if np.mean(np.min(pixels, axis=2)[ring] > 245) < .96 or np.mean(pixels[radius > .6]) >= 250:
            continue
        icon = np.asarray(_image(crop(observation.png, map_bounds(icons[0], observation.logical_size, observation.physical_size))))
        y, x = np.where(np.max(icon, axis=2) < 140)
        ih, iw = icon.shape[:2]
        if (not .06 <= len(x) / (ih * iw) <= .30 or not len(x)
                or not .55 <= (x.max() - x.min()) / iw <= .95
                or not .25 <= (y.max() - y.min()) / ih <= .65
                or abs((x.min() + x.max()) / 2 - iw / 2) > iw * .10):
            continue
        design = np.column_stack([np.ones(len(x)), np.abs(x - (x.min() + x.max()) / 2)])
        fit = np.linalg.lstsq(design, y, rcond=None)[0]
        if (not -1.5 <= fit[1] <= -.5 or not ih * .55 <= fit[0] <= ih * .90
                or np.sqrt(np.mean((design @ fit - y) ** 2)) > ih * .08):
            continue
        candidates.append(physical)
    return candidates[0] if len(candidates) == 1 else None


def capture_viewport(observation, base):
    """Exclude verified floating scroll buttons while retaining original pixels."""
    lw, lh = observation.logical_size
    blockers = []
    for node in observation.nodes:
        if (node.get("package") != "com.aurora.xiaohe.aidoctor" or not node.get("clickable")
                or _label(node) or node.get("resource_id") or node.get("class_name") not in ("android.view.View", "android.widget.ImageView")):
            continue
        box = node["bounds"]
        width, height = box[2] - box[0], box[3] - box[1]
        if not (lw * .075 <= width <= lw * .16 and .80 <= width / max(height, 1) <= 1.20
                and lw * .40 <= (box[0] + box[2]) / 2 <= lw * .60 and lh * .65 <= box[1] < box[3] <= lh * .92):
            continue
        physical = map_bounds(box, observation.logical_size, observation.physical_size)
        if base[1] < physical[1] < physical[3] <= base[3] + observation.physical_size[1] * .025 and base[3] - physical[3] <= observation.physical_size[1] * .12:
            blockers.append(physical)
    bottom = min([base[3], *(box[1] - observation.physical_size[1] * .004 for box in blockers)])
    direct = _direct_scroll_button(observation, base)
    if direct:
        blockers.append(direct)
        # The MiniApp has a large soft shadow outside its accessibility box.
        bottom = min(bottom, direct[1] - (direct[2] - direct[0]) * .5)
    bounds = (base[0], base[1], base[2], int(bottom))
    if bounds[3] - bounds[1] < observation.physical_size[1] * .35:
        raise CaptureError("浮动控件使正式正文视口过小，无法可靠采集")
    return bounds, {"occlusion_checked": True, "viewport_physical": list(bounds), "excluded_controls": [list(box) for box in blockers]}


def _parse_zh_number(raw):
    if raw.isdigit():
        return int(raw)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    value, pending = 0, 0
    for char in raw:
        if char in digits:
            pending = digits[char]
        elif char in "十百":
            value += (pending or 1) * (10 if char == "十" else 100)
            pending = 0
    return value + pending


def _reference_count(text):
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    return sum(_parse_zh_number(raw) for raw in re.findall(r"(" + NUMBER + r")(?:篇|份|条|部|本|项)", compact))


def _literature_count(text):
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    if _reference_count(compact) == 0:
        return None
    return sum(_parse_zh_number(raw) for raw in NON_MONOGRAPH_HEADER_ITEM.findall(compact))


def _is_monograph_row(line, all_lines, width):
    match = NUMBERED_CITATION_ROW.match(_label(line))
    title = match.group(2).strip() if match else _label(line)
    if MONOGRAPH_ROW_PATTERN.search(title):
        return True
    box = line["bounds"]
    row_h = max(box[3] - box[1], 1)
    row_cy = (box[1] + box[3]) / 2
    for other in all_lines:
        if other is line:
            continue
        obox = other["bounds"]
        if obox[0] < box[2] - width * .03:
            continue
        other_h = max(obox[3] - obox[1], 1)
        other_cy = (obox[1] + obox[3]) / 2
        if abs(other_cy - row_cy) <= max(row_h, other_h) * .85 and MONOGRAPH_TAG_PATTERN.fullmatch(_label(other).strip()):
            return True
    return False


def citation_detail_rows(observation, viewport, header=None, *, min_index=1, max_total=None):
    """Parse sequential expanded citation rows and separate literature from drug monographs."""
    lines = sorted(_ocr(observation), key=lambda item: (item["bounds"][1], item["bounds"][0]))
    width, height = observation.physical_size
    try:
        safe_viewport, _ = capture_viewport(observation, viewport)
        safe_bottom = safe_viewport[3]
    except CaptureError:
        safe_bottom = viewport[3]
    if header is not None and header["bounds"][3] >= viewport[1]:
        search_top = header["bounds"][3] - height * .008
        anchor_x = header["bounds"][0]
        start_num = 1
    else:
        search_top = viewport[1]
        anchor_x = None
        start_num = min_index
    rows, prev_num, prev_bottom = [], None, None
    for line in lines:
        box = line["bounds"]
        if box[1] < search_top or box[3] > viewport[3]:
            continue
        if anchor_x is not None and abs(box[0] - anchor_x) > width * .06:
            continue
        match = NUMBERED_CITATION_ROW.match(_label(line))
        if not match:
            continue
        num, title = int(match.group(1)), match.group(2).strip()
        if max_total and num > max_total:
            break
        if prev_num is None:
            if header is not None and header["bounds"][3] >= viewport[1]:
                if num != 1:
                    continue
            elif not (start_num <= num <= start_num + 2):
                continue
            anchor_x = box[0] if anchor_x is None else anchor_x
        else:
            if num != prev_num + 1 or box[1] - prev_bottom > height * .075:
                break
        monograph = _is_monograph_row(line, lines, width)
        safe = box[1] >= viewport[1] + height * .005 and box[3] <= safe_bottom
        rows.append({
            "index": num,
            "text": _label(line),
            "title": title,
            "bounds": tuple(box),
            "is_monograph": monograph,
            "safe": safe,
        })
        prev_num, prev_bottom = num, box[3]
    return {
        "rows": rows,
        "literature": [row for row in rows if not row["is_monograph"]],
        "monographs": [row for row in rows if row["is_monograph"]],
        "max_index": rows[-1]["index"] if rows else 0,
    }


def _citation_detail_loading(observation):
    if any(node.get("package") in ("", observation.package) and node.get("class_name", "").endswith("ProgressBar")
           for node in observation.nodes):
        return True
    return any(CITATION_DETAIL_LOADING.search(_label(line)) for line in _physical_labels(observation))


def _citation_detail_error(observation):
    if network_error_visible(observation):
        return True
    return any(CITATION_DETAIL_ERROR.search(_label(line)) for line in _physical_labels(observation))


def _citation_detail_valid(observation, list_title, expected_package):
    if observation.package != expected_package or answer_page_ready(observation):
        return False
    if _citation_detail_loading(observation) or _citation_detail_error(observation):
        return False
    w, h = observation.physical_size
    body_crop = _image(observation.png).crop((0, int(h * .12), w, int(h * .88)))
    if np.asarray(body_crop, dtype=np.float32).std(axis=(0, 1)).max() < 8.0:
        return False
    labels = [_label(line) for line in _physical_labels(observation) if line["bounds"][1] >= h * .06]
    joined = " ".join(labels)
    corpus = normalized(joined)
    if not (CITATION_DETAIL_CUES.search(joined) or len(corpus) >= 40):
        return False
    needle = normalized(re.sub(r"[…\.]+$", "", list_title or ""))
    if len(needle) >= 4:
        probe = needle[:min(len(needle), 16)]
        prefix = probe[:min(len(probe), 6)]
        if prefix not in corpus:
            windows = any(probe[i:i + 4] in corpus for i in range(max(0, len(probe) - 3)))
            char_overlap = sum(1 for ch in probe if ch in corpus) / len(probe)
            if not (windows and char_overlap >= .6):
                return False
    return True


def _extract_citation_detail_title(observation, list_title):
    h = observation.physical_size[1]
    needle = normalized(re.sub(r"[…\.]+$", "", list_title or ""))
    probe = needle[:min(len(needle), 6)]
    candidates = [_label(line) for line in _physical_labels(observation)
                  if h * .10 <= line["bounds"][1] <= h * .28 and 6 <= len(normalized(_label(line))) <= 80]
    if probe:
        matched = [text for text in candidates if probe in normalized(text)]
        if matched:
            return max(matched, key=lambda text: len(normalized(text)))
    return list_title


def _collapsed_reference_preview(observation, header, lines):
    """Two real reference previews do not prove a larger panel is expanded."""
    if _reference_count(header.get("canonical", header["text"])) <= 2:
        return False
    box = header["bounds"]
    height, width = observation.physical_size[1], observation.physical_size[0]
    rows = [line for line in lines if re.match(r"^[12][.、．]", _label(line))
            and box[3] <= line["bounds"][1] <= box[3] + height * .08]
    first = next((line for line in rows if re.match(r"^1[.、．]", _label(line))), None)
    second = next((line for line in rows if re.match(r"^2[.、．]", _label(line))), None)
    if not first or not second:
        return False
    following = sorted((line for line in lines if second["bounds"][3] < line["bounds"][1] <= second["bounds"][3] + height * .08
                        and abs(line["bounds"][0] - box[0]) <= width * .04), key=lambda line: line["bounds"][1])
    # An explicit upward indicator is stronger than the preview heuristic.
    upward = re.search(r"[\^∧︿⌃˄へ]\s*$", header["text"])
    return bool(not upward and following and not re.match(r"^3[.、．]", _label(following[0])))


def citation_state(observation, viewport, expected=None):
    lines = _ocr(observation)
    learned = role_labels(observation, "citations_header")
    if learned:
        header = learned[0]
        canonical = _exact_question(header["text"])
        box = header["bounds"]
        rows = [row for row in role_labels(observation, "citations_rows") if box[3] <= row["bounds"][1] < row["bounds"][3] <= viewport[3]
                and len(normalized(row["text"])) >= 6]
        # Learning a title does not require learning its changing article names.
        # Retain independently observed, adjacent numbered bibliography rows.
        rows.extend(line for line in lines if re.match(r"^\s*[12][.、．]", _label(line))
                    and box[3] <= line["bounds"][1] <= box[3] + observation.physical_size[1] * .075
                    and line["bounds"][3] <= viewport[3] and len(normalized(_label(line))) >= 6)
        bibliography = any(re.search(r"说明书|指南|共识|研究|分析|文献|规范|doi|pubmed|(?:19|20)\d{2}", row["text"], re.I) for row in rows)
        source = any(SOURCE.fullmatch(_label(line)) and any(abs(line["bounds"][1] - row["bounds"][1]) <= observation.physical_size[1] * .025 for row in rows) for line in lines)
        return {"present": True, "expanded": bool(rows) and (bibliography or source) and (expected is None or expected == canonical)
                and not _collapsed_reference_preview(observation, {**header, "canonical": canonical}, lines),
                "header": {**header, "canonical": canonical}, "method": "learned_roles_with_visible_reference_text"}
    headers = []
    for line in lines:
        box = line["bounds"]
        if not viewport[1] <= box[1] <= viewport[1] + (viewport[3] - viewport[1]) * .58:
            continue
        text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", _label(line)))
        plain = re.sub(r"[\^∧︿⌃˄∨⌄vVへ~～]+$", "", text)
        if re.fullmatch(TITLE, plain):
            headers.append({**line, "canonical": plain})
        elif text.startswith(("参考", "根据")) and re.search(NUMBER, text) and re.search(r"文献|资料|指南|共识|说明", text):
            raise CaptureError("发现未知引用资料标题，不能按无引用交付")
    if len(headers) > 1:
        raise CaptureError("视口包含多个引用标题，无法确认本题引用范围")
    if not headers:
        return {"present": False, "expanded": False, "header": None}
    header = headers[0]
    box = header["bounds"]
    rows = [line for line in lines if re.match(r"^\s*[12][.、．]", _label(line))
            and box[3] <= line["bounds"][1] <= box[3] + observation.physical_size[1] * .075]
    immediate = [line for line in rows if line["bounds"][1] <= box[3] + observation.physical_size[1] * .035]
    bibliography = any(re.search(r"说明书|指南|共识|研究|分析|文献|规范|(?:19|20)\d{2}|[…]{1,3}|\.{2,}$", _label(line)) for line in immediate)
    source = any(SOURCE.fullmatch(_label(label)) and abs(label["bounds"][1] - row["bounds"][1]) <= max(row["bounds"][3] - row["bounds"][1], label["bounds"][3] - label["bounds"][1])
                 for row in immediate for label in lines)
    sequential = {re.match(r"^\s*([12])", _label(line)).group(1) for line in rows} == {"1", "2"}
    expanded = ((bibliography or source or sequential) and (expected is None or header["canonical"] == expected)
                and not _collapsed_reference_preview(observation, header, lines))
    return {"present": True, "expanded": bool(expanded), "header": header}


def product_state(observation, viewport, pending=False, image_evidence=False, text_only_evidence=False):
    entries = [(line["text"], line["bounds"]) for line in _ocr(observation)]
    entries += [(_label(node), map_bounds(node["bounds"], observation.logical_size, observation.physical_size))
                for node in observation.nodes if node.get("package") in ("", observation.package)
                and _label(node) and 0 <= node["bounds"][0] < node["bounds"][2] <= observation.logical_size[0]
                and 0 <= node["bounds"][1] < node["bounds"][3] <= observation.logical_size[1]]
    entries += [(row["text"], row["bounds"]) for row in role_labels(observation, "products_entry")
                if re.fullmatch(PRODUCT_ENTRY, row["text"].strip())]
    entries += [("参考药品", row["bounds"]) for row in role_labels(observation, "products_header")]
    visible = [(text, box) for text, box in entries if viewport[1] <= (box[1] + box[3]) / 2 <= viewport[3]]
    if any(re.fullmatch(PRODUCT_ENTRY, text.strip()) for text, _ in visible):
        return {"present": True, "mode": "drawer", "complete": False, "reason": "expandable_product_list"}
    headings = [(text, box) for text, box in visible if re.fullmatch(PRODUCT_TITLE, text.strip())]
    if pending and not headings:
        headings = [("参考药品", (viewport[0], viewport[1], viewport[2], viewport[1]))]
    for text, box in headings:
        purchases = [candidate for other, candidate in visible if "去购买" in other and box[3] <= candidate[1] < box[3] + observation.physical_size[1] * .40]
        arrows = [node for node in observation.nodes if node.get("class_name", "").endswith("ImageView")
                  and node["bounds"][0] > observation.logical_size[0] * .70
                  and abs((node["bounds"][1] + node["bounds"][3]) / 2 * observation.physical_size[1] / observation.logical_size[1] - (box[1] + box[3]) / 2) < (box[3] - box[1]) * 1.5]
        if arrows:
            return {"present": True, "mode": "drawer", "complete": False, "reason": "product_list_arrow"}
        image_boxes = _product_image_boxes(observation, (viewport[0], box[1], viewport[2], viewport[3]))
        images_ready = all(np.asarray(_image(observation.png).crop(candidate), dtype=np.float32).std(axis=(0, 1)).max() >= 12 for candidate in image_boxes)
        aligned = {normalized(other) for other, candidate in visible if box[3] < candidate[1] < box[3] + observation.physical_size[1] * .25
                   and abs(candidate[0] - box[0]) <= observation.physical_size[0] * .04
                   and other.strip() not in ("去购买", "参考药品", "推荐药品") and len(normalized(other)) >= 2}
        full_width_name = any(normalized(_label(node)) in aligned and node["bounds"][2] - node["bounds"][0] >= observation.logical_size[0] * .65
                              for node in observation.nodes if _label(node) and node.get("package") in ("", observation.package))
        text_only = not image_boxes and (text_only_evidence or len(aligned) >= 2 and full_width_name)
        if not purchases:
            return {"present": True, "mode": "inline", "complete": False, "first_item_confirmed": False, "images_ready": images_ready,
                    "image_regions_checked": len(image_boxes), "text_only": text_only, "pending": True}
        last = max(candidate[3] for candidate in purchases)
        item_seen = any(other.strip() not in ("参考药品", "推荐药品", "去购买") and len(other.strip()) >= 2 and box[3] < candidate[1] < last for other, candidate in visible)
        image_boxes = [candidate for candidate in image_boxes if candidate[3] <= last]
        ready = images_ready and (bool(image_boxes) or image_evidence or text_only)
        return {"present": True, "mode": "inline", "complete": item_seen and ready, "first_item_confirmed": item_seen, "images_ready": ready,
                "image_regions_checked": len(image_boxes), "text_only": text_only, "image_requirement": "none" if text_only else "loaded_images",
                "text_only_evidence": "full_width_name_and_description" if text_only else None, "pending": False}
    return {"present": False, "mode": "none", "complete": True, "first_item_confirmed": True, "end_confirmed": True, "images_ready": True, "seams_verified": True}


def _physical_labels(observation):
    labels = list(_ocr(observation))
    labels += [{"text": _label(node), "bounds": map_bounds(node["bounds"], observation.logical_size, observation.physical_size)}
               for node in observation.nodes if node.get("package") in ("", observation.package) and _label(node)
               and 0 <= node["bounds"][0] < node["bounds"][2] <= observation.logical_size[0]
               and 0 <= node["bounds"][1] < node["bounds"][3] <= observation.logical_size[1]]
    return labels


def drawer_state(observation):
    """Identify an overlay by its title and separate list below that title."""
    w, h = observation.physical_size
    labels = _physical_labels(observation)
    headings = [line for line in labels if re.fullmatch(PRODUCT_TITLE, _label(line))]
    headings += [{**row, "text": "参考药品"} for row in role_labels(observation, "products_header")]
    candidates = []
    for header in headings:
        hb = header["bounds"]
        for node in observation.nodes:
            is_list = node.get("scrollable") or node.get("class_name", "").endswith("RecyclerView")
            if node.get("package") not in ("", observation.package) or not is_list:
                continue
            box = map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
            if (box[2] - box[0] >= w * .75 and box[3] >= h * .85 and box[3] - box[1] >= h * .22
                    and hb[3] - h * .025 <= box[1] <= hb[3] + h * .10 and hb[1] < h * .70):
                candidates.append((header, box))
    if not candidates:
        return None
    header, viewport = min(candidates, key=lambda pair: pair[1][1])
    hb = header["bounds"]
    panels = [map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
              for node in observation.nodes if node.get("package") in ("", observation.package)]
    panels = [box for box in panels if box[0] <= min(hb[0], viewport[0]) and box[2] >= max(hb[2], viewport[2])
              and 0 <= hb[1] - box[1] <= h * .10 and box[3] >= viewport[3]]
    panel = min(panels, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]), default=None)
    panel_top = panel[1] if panel else hb[1]
    # A complete top card needs its name below the actual list boundary.
    names = [line for line in labels if viewport[0] <= line["bounds"][0] < line["bounds"][2] <= viewport[2]
             and viewport[1] < line["bounds"][1] < viewport[1] + h * .16
             and len(normalized(_label(line))) >= 2
             and not re.fullmatch(r"参考药品|推荐药品|处方药|非处方药|OTC|医保[甲乙丙]类|去购买|购买|关闭|返回|拖动柄", _label(line), re.I)
             and not re.match(r"^(?:参考价|[¥￥]|[0-9]+(?:\.[0-9]+)?\s*(?:mg|ml|g|片|粒|袋|支|盒)(?:\W|$))", _label(line), re.I)]
    names.sort(key=lambda line: (line["bounds"][1], line["bounds"][0]))
    return {"header": header, "viewport": viewport, "panel_top": panel_top,
            "expanded": panel_top < h * .20 and viewport[3] - viewport[1] >= h * .60,
            "first_item": normalized(_label(names[0])) if names else None,
            "first_item_bounds": names[0]["bounds"] if names else None}


def _product_image_boxes(observation, viewport, *, canvas_cards=False):
    """ImageView or unlabelled square Compose leaf regions, in physical pixels."""
    w, h = observation.logical_size
    candidates = set()
    for node in observation.nodes:
        if node.get("package") not in ("", observation.package) or _label(node):
            continue
        box = node["bounds"]
        bw, bh = box[2] - box[0], box[3] - box[1]
        if not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h and w * .075 <= bw <= w * (.5 if node.get("layout_hint") else .40) and .6 <= bw / max(bh, 1) <= 1.7):
            continue
        image_class = node.get("class_name", "").endswith("ImageView")
        if not image_class and not node.get("class_name", "").endswith(".View"):
            continue
        if any(_label(child) and box[0] <= child["bounds"][0] < child["bounds"][2] <= box[2]
               and box[1] <= child["bounds"][1] < child["bounds"][3] <= box[3] for child in observation.nodes):
            continue
        physical = map_bounds(box, observation.logical_size, observation.physical_size)
        if (viewport[0] <= physical[0] < physical[2] <= viewport[2]
                and viewport[1] <= physical[1] < physical[3] <= viewport[3]):
            candidates.add(tuple(physical))
    if canvas_cards:
        # Some pages draw medicine photos directly inside card containers.
        labels = _ocr(observation)
        pw, ph = observation.physical_size
        lists = [map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
                 for node in observation.nodes if node.get("package") in ("", observation.package)
                 and node.get("class_name", "").endswith("RecyclerView")]
        # Restrict the clipping check to horizontal descendants of this drawer,
        # including a one-card row that Android marks as non-scrollable.
        horizontal_rows = product_carousels(observation, viewport, include_static=True) if lists else []
        for node in observation.nodes:
            if node.get("package") not in ("", observation.package) or node.get("class_name") != "android.widget.FrameLayout":
                continue
            box = map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
            bw, bh = box[2] - box[0], box[3] - box[1]
            if (bw >= pw * .75 and ph * .08 <= bh <= ph * .25
                    and viewport[0] <= box[0] < box[2] <= viewport[2] and viewport[1] <= box[1] < box[3] <= viewport[3]
                    and any(area[0] <= box[0] and box[2] <= area[2] and area[1] <= box[1] < box[3] <= area[3] for area in lists)):
                # A clipped carousel row can be as short as a compact list item.
                # Its centre is not a photo. Use the actual card/photo evidence below.
                if ((box[1] == viewport[1] or box[3] == viewport[3])
                        and any(box[0] <= l < r <= box[2] and box[1] <= t < b <= box[3] and r-l >= bw*.8
                                for l, t, r, b in horizontal_rows)):
                    continue
                if bh < ph * .12 and (box[1] <= viewport[1] + ph * .005 or box[3] >= viewport[3] - ph * .005):
                    continue
                side = min(round(bh * .5), round(pw * .18))
                left = box[0] + round(bw * .03)
                top = box[1] + (bh - side) // 2
                candidates.add((left, top, left + side, top + side))
        # Douyin cards use the repeated name and CTA to bound a larger photo.
        for node in observation.nodes:
            if node.get("package") not in ("", observation.package) or _label(node) or node.get("class_name") != "android.view.ViewGroup":
                continue
            box = map_bounds(node["bounds"], observation.logical_size, observation.physical_size)
            bw, bh = box[2] - box[0], box[3] - box[1]
            if not (pw * .15 <= bw <= pw * .5 and .35 <= bw / max(bh, 1) <= .85
                    and viewport[0] <= box[0] < box[2] <= viewport[2] and viewport[1] <= box[1] < box[3] <= viewport[3]):
                continue
            names = [line for line in labels if 0 <= box[1] - line["bounds"][3] <= ph * .06
                     and len(normalized(_label(line))) >= 2]
            for name in names:
                inside = sorted([line for line in observation.texts if line.get("confidence", 0) >= .50
                                 and box[0] <= line["bounds"][0] < line["bounds"][2] <= box[2]
                                 and box[1] + bh * .35 <= line["bounds"][1] < line["bounds"][3] <= box[1] + bh * .85],
                                key=lambda line: (line["bounds"][1], line["bounds"][0]))
                repeats, expected = [], normalized(_label(name))
                for index, line in enumerate(inside):
                    if line.get("confidence", 0) < .78:
                        continue
                    combined, previous = "", None
                    for part in inside[index:]:
                        if previous and (part["bounds"][1] - previous["bounds"][3] > ph * .016
                                         or abs(part["bounds"][0] - line["bounds"][0]) > pw * .025):
                            break
                        combined += normalized(_label(part))
                        if combined == expected or (_label(part).rstrip().endswith(("...", "…")) and expected.startswith(combined)):
                            repeats.append(line)
                            break
                        if not expected.startswith(combined):
                            break
                        previous = part
                for repeated in repeats:
                    buttons = [line for line in labels if re.fullmatch(r"查看说明书|药物指南", _label(line))
                               and box[0] <= line["bounds"][0] < line["bounds"][2] <= box[2]
                               and repeated["bounds"][3] < line["bounds"][1] < line["bounds"][3] <= box[3]]
                    photo_bottom = repeated["bounds"][1] - bw * .02
                    if buttons and .6 <= bw / max(photo_bottom - box[1], 1) <= 1.7:
                        candidates.add((box[0], box[1], box[2], int(photo_bottom)))
    return sorted(candidates, key=lambda box: (box[1], box[0]))


def product_image_evidence(observation, viewport):
    boxes = _product_image_boxes(observation, viewport, canvas_cards=True)
    loading = any(re.search(r"加载中|加载失败|重新加载|点击重试", _label(line)) and viewport[1] <= line["bounds"][1] < viewport[3]
                  for line in _physical_labels(observation))
    image = _image(observation.png)
    missing = []
    for box in boxes:
        pixels = np.asarray(image.crop(box), dtype=np.float32)
        h, w = pixels.shape[:2]
        interior = pixels[round(h*.10):round(h*.90), round(w*.10):round(w*.90)]
        if not interior.size or interior.std(axis=(0, 1)).max() < 12:
            missing.append(box)
    return {"ready": bool(boxes) and not loading and not missing, "regions": boxes,
            "loading_indicator": loading, "unready_regions": missing}


def _product_images_ready(observation, viewport):
    evidence = product_image_evidence(observation, viewport)
    return evidence["ready"], evidence["regions"]


def _answer_content_visible(observation, viewport, question):
    expected = normalized(question)
    question_box = question_bubble(observation, question, viewport) if expected else None
    content_top = question_box["bounds"][3] if question_box else viewport[1]
    for line in _physical_labels(observation):
        text, box = _label(line), line["bounds"]
        value = normalized(text)
        if not content_top <= box[1] < box[3] <= viewport[3] or len(value) < 6 or value in expected:
            continue
        if (re.match(r"^(?:小荷AI医生|AI回答仅供参考|AI生成|停止生成|正在思考|生成中|正在生成|加载中|你还可以问|猜你想问)", text, re.I)
                or re.fullmatch(r"(?:发送消息|按住说话|输入问题|输入消息|开启新会话|新会话|报告解读|立即下载|拍患处|拍报告|免费咨询|参考药品|推荐药品)", text)
                or re.fullmatch(TITLE, re.sub(r"\s+", "", text)) or value.isdigit()):
            continue
        if re.search(r"[A-Za-z\u4e00-\u9fff]", text):
            return True
    return False


def network_error_visible(observation):
    labels = [_label(line) for line in _physical_labels(observation)]
    return any(re.fullmatch(r"网络不稳定[，,]?请重试", text) for text in labels) and "重试" in labels


def _visible_products_entry(observation, viewport):
    """Return only a genuine visible all/more-medicines label, never a role alias."""
    candidates = [line for line in _physical_labels(observation)
                  if re.fullmatch(PRODUCT_ENTRY, _label(line))
                  and viewport[0] <= line["bounds"][0] < line["bounds"][2] <= viewport[2]
                  and viewport[1] < line["bounds"][1] < line["bounds"][3] < viewport[3]]
    unique = []
    for line in candidates:
        box = line["bounds"]
        if not any(abs(box[0] - other["bounds"][0]) < observation.physical_size[0] * .04
                   and abs(box[1] - other["bounds"][1]) < observation.physical_size[1] * .02
                   for other in unique):
            unique.append(line)
    return unique[0] if len(unique) == 1 else None


class AnswerCapture:
    def __init__(self, device, engine, *, answer_page_check=None, answer_page_error=None):
        self.device, self.engine = device, engine
        self.answer_page_check = answer_page_check
        self.answer_page_error = answer_page_error
        self._clock, self._sleep = time.monotonic, time.sleep
        self.quiet_seconds = 5.0
        self.settle_seconds = 12.0
        self.generation_timeout_seconds = 90.0
        self.timeout_seconds = 180.0
        self._last = None

    def _observe(self, ocr=False):
        observation = self.device.observe(ocr=ocr)
        if hasattr(self.engine, "decorate"):
            observation = self.engine.decorate(observation)
        self._last = observation
        if observation.package != self.package:
            raise CaptureError("采集期间前台应用发生变化，已停止操作")
        validate_geometry(observation.logical_size, observation.physical_size)
        if hasattr(self, "geometry") and (tuple(observation.logical_size), tuple(observation.physical_size)) != self.geometry:
            raise CaptureError("采集期间窗口尺寸改变，不能拼接不同坐标空间")
        if hasattr(self.engine, "review_capture"):
            observation = self.engine.review_capture(observation)
            self._last = observation
        check, message = self.answer_page_check, self.answer_page_error
        if check is None:
            from .platforms import PLATFORMS
            platform = next((item for item in PLATFORMS.values() if item.PACKAGE == self.package), None)
            if platform is not None:
                check, message = platform.answer_ready, platform.ANSWER_PAGE_ERROR
        if check is not None and not check(observation) and not ocr:
            # Some mini-app headers are drawn without hierarchy text. Verify
            # their real screenshot before treating the page as a different app.
            self.device.recognize(observation)
            if hasattr(self.engine, "decorate"):
                observation = self.engine.decorate(observation)
        if check is not None and not check(observation):
            raise CaptureError(message or "当前页面不属于已确认的目标平台回答页，不能继续滚动")
        if guide_visible(observation):
            observation = self._dismiss_guide(observation)
        return observation

    def _dismiss_guide(self, observation, pending=False):
        if not pending and not guide_visible(observation):
            return observation
        self._last = observation
        self._save("guide-overlay", observation)
        self.engine.log("guide_overlay_detected", step="overlay.dismiss")
        pending_file = self.engine.state.get("pending_request")
        if pending_file:
            request = read_json(Path(pending_file))
            if (request.get("step") != "overlay.dismiss" or request.get("package") != self.package
                    or request.get("entry") != self.engine.state.get("entry") or request.get("question_index") != self.engine.state.get("index")):
                raise CaptureError("引导浮层与当前修复断点冲突，保留现场且不重放其他步骤")
        def restored(item):
            if item.package != self.package or guide_visible(item):
                return False
            try:
                viewport = answer_viewport(item)
            except CaptureError:
                return False
            lw, lh = item.logical_size
            input_seen = any(node.get("package") in ("", self.package) and node["bounds"][1] > lh * .6
                             and (node.get("class_name", "").endswith("EditText") or re.match(r"^(?:输入问题|按住说话|发送消息|输入消息)", _label(node)))
                             for node in item.nodes)
            body_seen = any(node.get("package") in ("", self.package) and node["bounds"][2] - node["bounds"][0] >= lw * .65
                            and node["bounds"][3] - node["bounds"][1] >= lh * .35
                            and abs(node["bounds"][1] * item.physical_size[1] / lh - viewport[1]) <= item.physical_size[1] * .025
                            for node in item.nodes)
            return input_seen and body_seen
        result, resolved_by = None, "verified_step"
        if not pending_file:
            deadline = self._clock() + 5
            for probe in range(1, 4):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._sleep(min(1.0, remaining))
                after = self.device.observe(ocr=False)
                if after.package != self.package:
                    raise CaptureError("等待引导消失期间前台应用改变，已停止操作")
                validate_geometry(after.logical_size, after.physical_size)
                if hasattr(self, "geometry") and (tuple(after.logical_size), tuple(after.physical_size)) != self.geometry:
                    raise CaptureError("等待引导消失期间窗口尺寸改变，停止采集")
                self._last = after
                self.engine.log("guide_overlay_wait", probe=probe, read_only=True)
                if restored(after):
                    result, resolved_by = after, "read_only_wait"
                    break
        if result is None:
            result = self.engine.perform("overlay.dismiss", "关闭“在这里查看历史对话”引导：点击安全空白区域或明确的知道了按钮，不打开历史会话、不聚焦输入、不发送", self.package,
                                         restored, defaults=(), hints=(GUIDE_HINT, r"^(?:知道了|我知道了|关闭引导)$"), risk="toggle")
        if not restored(result):
            raise CaptureError("引导关闭后真实输入栏及正文层级未恢复")
        validate_geometry(result.logical_size, result.physical_size)
        if hasattr(self, "geometry") and (tuple(result.logical_size), tuple(result.physical_size)) != self.geometry:
            raise CaptureError("关闭引导期间窗口尺寸改变，停止采集")
        self._last = result
        self._overlay_revision = getattr(self, "_overlay_revision", 0) + 1
        self.engine.log("guide_overlay_dismissed", underlying_layout_verified=True, method=resolved_by)
        return result

    def _save(self, label, observation):
        self.counter += 1
        directory = self.attempt / f"{self.counter:03d}-{label}"
        save_observation(directory, observation)
        return str(directory.resolve())

    def _region(self, observation):
        return crop(observation.png, self.viewport)

    def _answer_viewport(self, observation):
        def check(item):
            try:
                return answer_viewport(item)
            except CaptureError:
                return None
        return self.engine.layout("answer_container", observation, check, request=True)

    def _drawer_state(self, observation, *, request=False):
        return self.engine.layout("product_list", observation, drawer_state, request=request)

    def _ready_images(self, observation, *, request=False):
        def check(item):
            ready, boxes = _product_images_ready(item, self.viewport)
            return boxes if ready else None
        return self.engine.layout("product_images", observation, check, request=request)

    def _close_existing_drawer(self, observation):
        if not self._drawer_state(observation):
            self.engine.resolve_capture_request("capture.close_existing_products")
            return observation
        self.engine.log("existing_products_drawer_detected", body_started=False)
        def restored(item):
            if item.package != self.package or self._drawer_state(item):
                return False
            try:
                answer_viewport(item)
                return True
            except CaptureError:
                return False
        return self.engine.perform("capture.close_existing_products", "采集开始前关闭已打开的药品浮层，恢复原回答；点击抽屉关闭按钮，不使用可能退出小程序的系统返回", self.package,
                                   restored, defaults=(), hints=(r"^(?:关闭|close)$",), risk="toggle")

    def _settled(self, initial=None, *, deadline=None):
        started = self._clock()
        current = self._dismiss_guide(initial).png if initial is not None else self.device.screenshot()
        expires = deadline if deadline is not None else started + self.settle_seconds
        probes, observations = 1, 0
        extended = deadline is not None
        while True:
            if self._clock() >= expires:
                # A long animation or a late image can outlast one window. Extend
                # once with the same stability rules before giving up.
                if extended:
                    break
                extended = True
                expires = self._clock() + self.settle_seconds
                self.engine.log("frame_settle_extended", seconds=round(self._clock() - started, 3), png_probes=probes)
            self._sleep(.25)
            probe = self.device.screenshot()
            probes += 1
            if not stable(current, probe, bounds=self.viewport):
                current = probe
                continue
            # ADB PNG → guarded hierarchy + final ADB PNG. The final observation
            # still verifies app, lock, rotation and both coordinate spaces.
            after = self._observe()
            observations += 1
            if stable(probe, after.png, bounds=self.viewport):
                self.device.recognize(after)
                if hasattr(self.engine, "decorate"):
                    after = self.engine.decorate(after)
                if not guide_visible(after):
                    self.engine.log("frame_settled", seconds=round(self._clock() - started, 3),
                                    png_probes=probes, full_observations=observations,
                                    method="adb_png_hierarchy_png", stable_pairs=2)
                    return after
                after = self._dismiss_guide(after)
            current = after.png
        raise CaptureError("回答视口持续变化，无法取得稳定无损帧")

    def _swipe(self, direction, fraction=.43, alternate=False, *, duration=.4):
        left, top, right, bottom = self.viewport
        height = bottom - top
        x = left + (right - left) * (.68 if alternate else .84)
        distance = height * min(.70, max(.07, fraction))
        start_y = top + height * .82 if direction == "down" else top + height * .16
        end_y = start_y - distance if direction == "down" else start_y + distance
        lw, lh = self.geometry[0]
        pw, ph = self.geometry[1]
        self.device.swipe((x * lw / pw, start_y * lh / ph), (x * lw / pw, end_y * lh / ph), duration=duration, expected_package=self.package)
        return distance

    def _stride(self, seams):
        recent = seams[-2:]
        height = self.viewport[3] - self.viewport[1]
        if len(recent) < 2 or any(not s.get("verified") or s["score"] < .95 or s["overlap"] < height * .35 for s in recent):
            return .43
        return min(.56, max(.43, min(s["shift"] for s in recent) / height + .08))

    def _complete(self, current, question):
        deadline = self._clock() + self.generation_timeout_seconds
        quiet_since, unchanged = self._clock(), 0
        content_seen = _answer_content_visible(current, self.viewport, question)
        direct = self.engine.state.get("entry") == "douyin-direct"
        end_observations = [self._save("tail-initial", current)] if direct else []
        while self._clock() < deadline:
            self._swipe("down", .65, unchanged > 0)
            after = self._settled(deadline=deadline)
            if network_error_visible(after):
                after = self._recover_network(after)
            loading = any(re.search(r"停止生成|正在思考|生成中|正在生成|加载中|加载失败|重新加载", _label(line)) for line in _physical_labels(after))
            loading = loading or any(node.get("class_name", "").endswith("ProgressBar") for node in after.nodes)
            content_seen = content_seen or _answer_content_visible(after, self.viewport, question)
            if not loading and stable(self._region(current), self._region(after)):
                unchanged += 1
            else:
                unchanged, quiet_since = 0, self._clock()
            current = after
            if direct:
                end_observations.append(self._save("tail-probe", current))
            if content_seen and unchanged >= 2 and self._clock() - quiet_since >= self.quiet_seconds:
                self.engine.log("answer_generation_complete", probes=unchanged, quiet_seconds=self._clock() - quiet_since, answer_content_confirmed=True)
                self._latest_tail_evidence = self._save("generation-complete", current)
                self._direct_tail_ready = self.engine.state.get("entry") == "douyin-direct"
                self._direct_end_observations = end_observations[-3:]
                return current
            self._sleep(.4)
        raise CaptureError("未确认实际回答正文、仍在生成或未证明真实到底，拒绝提前采集")

    def _direct_identity_verified(self, question):
        from .platforms import douyin_direct
        return douyin_direct.capture_identity(self.engine, question)

    def _top(self, current, question):
        deadline, unchanged = self._clock() + 90, 0
        top_observations = []
        direct = self.engine.state.get("entry") == "douyin-direct"
        if direct:
            if not self._direct_identity_verified(question) or not getattr(self, "_direct_tail_ready", False):
                raise CaptureError("直接入口缺少本次发送证据或最新回答末尾证据，不能查找历史同题")
            self._direct_tail_ready = False
            self._direct_backtrack = [{"observation": self._save("latest-tail-start", current)}]
        while self._clock() < deadline:
            target = direct_question_bubble(current, question, self.viewport) if direct else question_bubble(current, question, self.viewport)
            if target:
                return current, target
            self.engine.log("question_top_not_confirmed", exact_question_found=False, unchanged=unchanged)
            if unchanged >= 2:
                source = self._search_source(question)
                if source and _answer_content_visible(current, self.viewport, question):
                    top_observations.append(self._save("answer-top", current))
                    return current, {"bounds": self.viewport, "method": "search_entry_and_verified_answer_top",
                                     "question_visible": False, "source_observation": str(source),
                                     "top_observations": top_observations[-3:]}
                raise CaptureError("已到历史会话顶部但未找到本题完整问题气泡")
            top_observations.append(self._save("top-search", current))
            fragment = latest_user_bubble(current, self.viewport) if direct else None
            direction = "down" if fragment and fragment["clipped_bottom"] else "up"
            self._swipe(direction, .12 if fragment else .43 if direct else .65, unchanged > 0)
            after = self._settled()
            if direct:
                path = self._save("latest-question-backtrack", after)
                seam = direct_scroll_seam(current, after, self.viewport, direction)
                self._direct_backtrack.append({"observation": path, "seam": seam})
            unchanged = unchanged + 1 if stable(self._region(current), self._region(after)) else 0
            current = after
        raise CaptureError("90秒内未定位到本题完整问题气泡")

    def _search_source(self, question):
        from .platforms import PLATFORMS
        platform = PLATFORMS.get(self.engine.state.get("entry"))
        source = self.attempt.parent / "entry-source"
        if platform is None or not platform.REENTER_SUPPORTED or not (source / "observation.json").is_file():
            return None
        observation = load_observation(source)
        if (platform.PACKAGE == self.package and platform.results_loaded(observation, question)
                and (platform.brand_entry_defaults(observation) or platform.platform_entry_visible(observation))):
            return source
        return None

    def _question_start(self, current, question):
        step = "capture.question_top"
        if self.engine.state.get("entry") == "douyin-direct":
            if not self._direct_identity_verified(question):
                raise CaptureError("直接入口缺少已验证的本次发送证据，不能改用全局同题匹配")
            if not getattr(self, "_direct_tail_ready", False):
                current = self._complete(current, question)
            current, _ = self._top(current, question)
            return self._position(current, question)
        def ready(item):
            if (item.package != self.package or not answer_page_ready(item)
                    or (tuple(item.logical_size), tuple(item.physical_size)) != self.geometry
                    or (self.answer_page_check is not None and not self.answer_page_check(item))):
                return False
            target = question_bubble(item, question, self.viewport)
            return bool(target and (target["bounds"][1] - self.viewport[1]) / (self.viewport[3] - self.viewport[1]) <= .20)

        if self._search_source(question) or not hasattr(self.engine, "prefer_goal") or not self.engine.prefer_goal(step, current):
            try:
                current, target = self._top(current, question)
                if target.get("question_visible") is False:
                    self.engine.resolve_capture_request(step)
                    return current, target
                return self._position(current, question)
            except CaptureError:
                if not hasattr(self.engine, "prefer_goal"):
                    raise
        current = self.engine.perform(step, "取得包含本题完整原题的正文首屏；可选择回顶按钮、导航或页面角色；不新建会话、不输入、不发送，仍须完整原题位于首屏顶部", self.package, ready)
        self._question_method_used = True
        current = self._settled(current)
        if not ready(current):
            raise CaptureError("导航后首屏未保留完整原题，不能开始正式截图")
        return current, question_bubble(current, question, self.viewport)

    def _position(self, current, question):
        direct = self.engine.state.get("entry") == "douyin-direct"
        if direct and not self._direct_identity_verified(question):
            raise CaptureError("直接入口原题定位缺少本次发送证据")
        if not question_bubble(current, question, self.viewport) and self._search_source(question):
            current, target = self._top(current, question)
            if target.get("question_visible") is False:
                return current, target
        for attempt in range(5):
            target = direct_question_bubble(current, question, self.viewport) if direct else question_bubble(current, question, self.viewport)
            if not target:
                if direct:
                    raise CaptureError("已核对的本次问题在首帧定位时丢失，不能继续扫描旧同题")
                self._swipe("up", .12, attempt > 0)
            else:
                ratio = (target["bounds"][1] - self.viewport[1]) / (self.viewport[3] - self.viewport[1])
                if ratio <= .20:
                    return current, target
                self._swipe("down", min(.25, ratio - .10))
            current = self._settled()
        raise CaptureError("无法将完整本题问题定位到正式首帧顶部")

    def _citations(self, current, pending=False):
        try:
            state = citation_state(current, self.viewport)
        except CaptureError:
            if not hasattr(self.engine, "perception"):
                raise
            current = self.engine.perception(current, "引用标题或展开列表发生变化，绑定citations_header及当前可见citations_rows")
            state = citation_state(current, self.viewport)
        if pending and not state["present"]:
            raise CaptureError("引用修复断点的原始标题已不可见，拒绝先滚动改变修复现场")
        if not state["present"] or state["expanded"] and not pending:
            return current, state
        canonical = state["header"]["canonical"]
        default = {"actions": [{"type": "tap", "target": {"kind": "ocr", "pattern": TITLE_HINT, "region": [0, self.viewport[1] / current.physical_size[1], 1, (self.viewport[1] + (self.viewport[3] - self.viewport[1]) * .58) / current.physical_size[1]], "offset": [.5, .5]}}]}
        if state.get("method"):
            default = {"actions": [{"type": "tap", "target": {"kind": "ocr", "pattern": "^" + re.escape(state["header"]["text"]) + "$"}}]}
        self.engine.log("citation_expansion_requested", risk="toggle")
        result = self.engine.perform("citation.expand", "保留当前回答展开后的真实引用资料；可逐步导航或重新识别，完成条件是同一引用标题及独立资料行", self.package,
                                     lambda observation: observation.package == self.package and citation_state(observation, self.viewport, canonical)["expanded"],
                                     defaults=(default,), hints=(TITLE_HINT,), risk="toggle")
        current = self._settled(result)
        verified = citation_state(current, self.viewport, canonical)
        if not verified["expanded"]:
            raise CaptureError("引用点击后未能独立确认真实资料行，不能重放切换控件")
        self.engine.log("citations_expanded", independent_rows_verified=True)
        return current, verified

    def _safe_leave_citation_detail(self, detail_obs):
        try:
            self._return_from_citation_detail(detail_obs)
        except Exception:
            pass

    def _wait_citation_detail(self, source_obs, row, tap_point):
        timeout = self.settle_seconds if self.settle_seconds < 5.0 else max(self.settle_seconds, 18.0)
        started = self._clock()
        deadline = started + timeout
        retry_after = min(3.0, timeout * .35)
        retried_tap = False
        entered_detail = False
        last_obs = source_obs
        while self._clock() < deadline:
            obs = self.device.observe(ocr=False)
            self._last = obs
            last_obs = obs
            if obs.package != self.package:
                raise CaptureError(f"点击参考文献第{row['index']}篇后前台应用发生变化，已停止采集")
            validate_geometry(obs.logical_size, obs.physical_size)
            if (tuple(obs.logical_size), tuple(obs.physical_size)) != self.geometry:
                raise CaptureError(f"点击参考文献第{row['index']}篇后窗口尺寸变化，停止采集")
            still_chat = obs.activity == source_obs.activity and answer_page_ready(obs)
            if still_chat:
                if not retried_tap and self._clock() - started >= retry_after and stable(source_obs.png, obs.png, bounds=self.viewport):
                    self.device.tap(tap_point, expected_package=self.package)
                    retried_tap = True
                self._sleep(.3)
                continue
            entered_detail = True
            w, h = obs.physical_size
            content_bounds = (0, int(h * .06), w, int(h * .96))
            body_std = np.asarray(_image(obs.png).crop((0, int(h * .12), w, int(h * .88))), dtype=np.float32).std(axis=(0, 1)).max()
            if body_std < 8.0:
                self._sleep(.3)
                continue
            self.device.recognize(obs)
            if hasattr(self.engine, "decorate"):
                obs = self.engine.decorate(obs)
            self._last = obs
            last_obs = obs
            if _citation_detail_error(obs):
                self._save(f"citation-detail-{row['index']:02d}-error", obs)
                self._safe_leave_citation_detail(obs)
                self._last = obs
                raise CaptureError(f"参考文献第{row['index']}篇详情页加载异常，拒绝交付异常截图")
            if _citation_detail_valid(obs, row["title"], self.package):
                self._sleep(.25)
                probe = self.device.screenshot()
                if stable(obs.png, probe, bounds=content_bounds):
                    return obs
                continue
            self._sleep(.3)
        self._save(f"citation-detail-{row['index']:02d}-timeout", last_obs)
        if entered_detail:
            self._safe_leave_citation_detail(last_obs)
        self._last = last_obs
        if not entered_detail:
            raise CaptureError(f"点击参考文献第{row['index']}篇后未能进入详情页")
        raise CaptureError(f"参考文献第{row['index']}篇详情页超时未完成加载或标题不匹配")

    def _return_from_citation_detail(self, detail_obs):
        timeout = max(self.settle_seconds, 1.0)
        started = self._clock()
        deadline = started + timeout
        retry_after = min(3.0, timeout * .35)
        self.device.press("back", expected_package=self.package)
        retried_back = False
        while self._clock() < deadline:
            self._sleep(.3)
            obs = self.device.observe(ocr=True)
            if hasattr(self.engine, "decorate"):
                obs = self.engine.decorate(obs)
            self._last = obs
            if obs.package != self.package:
                raise CaptureError("从参考文献详情返回后前台应用发生变化")
            validate_geometry(obs.logical_size, obs.physical_size)
            if (tuple(obs.logical_size), tuple(obs.physical_size)) != self.geometry:
                raise CaptureError("从参考文献详情返回后窗口尺寸发生变化")
            back_in_answer = answer_page_ready(obs) and (self.answer_page_check is None or self.answer_page_check(obs))
            if back_in_answer:
                return self._settled(obs)
            if not retried_back and self._clock() - started >= retry_after:
                self.device.press("back", expected_package=self.package)
                retried_back = True
        raise CaptureError("查看参考文献详情后未能返回原回答页")

    def _capture_citation_details(self, current, question, citations):
        if self.engine.state.get("entry") != "douyin-direct":
            return current, None
        if not citations.get("present") or not citations.get("header"):
            return current, {"required": False, "present": False, "complete": True, "header": None,
                             "expected_count": 0, "skipped_monographs": 0, "items": [], "image_paths": []}
        canonical = citations["header"]["canonical"]
        expected_lit = _literature_count(canonical)
        total_refs = _reference_count(canonical)
        debug_root = self.attempt.parent
        identity = [question, self.package, self.engine.state.get("entry"), self.engine.state.get("index")]

        # Reuse valid detail observations already captured for this question before a pause.
        captured_by_index = {}
        prepared = self.engine.state.get("capture_preparation") or {}
        saved_cd = prepared.get("citation_details") if prepared.get("identity") == identity else None
        if isinstance(saved_cd, dict) and saved_cd.get("header") == canonical:
            for item in saved_cd.get("items", []):
                try:
                    obs_path = Path(item["observation"]).resolve()
                    if not obs_path.is_relative_to(debug_root):
                        continue
                    saved_obs = load_observation(obs_path)
                    if _citation_detail_valid(saved_obs, item.get("list_title", ""), self.package):
                        captured_by_index[int(item["index"])] = dict(item)
                except (OSError, KeyError, TypeError, ValueError):
                    continue

        def persist_progress():
            ordered = [captured_by_index[idx] for idx in sorted(captured_by_index)]
            prep = self.engine.state.get("capture_preparation")
            if not isinstance(prep, dict) or prep.get("identity") != identity:
                prep = {"identity": identity, "stage": "citations"}
            prep["citation_details"] = {"header": canonical, "expected_count": expected_lit, "items": ordered}
            self.engine.state["capture_preparation"] = prep
            self.engine.save()

        self.engine.state["capture_objective"] = "citation.details"
        parsed = citation_detail_rows(current, self.viewport, citations["header"], max_total=total_refs or None)
        if expected_lit == 0 and not parsed["literature"]:
            return current, {"required": False, "present": False, "complete": True, "header": canonical,
                             "expected_count": 0, "skipped_monographs": len(parsed["monographs"]),
                             "items": [], "image_paths": []}

        seen_monographs = {row["index"] for row in parsed["monographs"]}
        max_seen_index = parsed["max_index"]
        scrolled = False

        for _ in range(7):
            live_citations = citation_state(current, self.viewport, canonical)
            header = live_citations["header"] if live_citations.get("present") else None
            parsed = citation_detail_rows(
                current, self.viewport, header,
                min_index=max(1, max_seen_index - 1) if header is None else 1,
                max_total=total_refs or None,
            )
            seen_monographs.update(row["index"] for row in parsed["monographs"])
            max_seen_index = max(max_seen_index, parsed["max_index"])

            while True:
                live_citations = citation_state(current, self.viewport, canonical)
                header = live_citations["header"] if live_citations.get("present") else None
                parsed = citation_detail_rows(
                    current, self.viewport, header,
                    min_index=max(1, max_seen_index - 1) if header is None else 1,
                    max_total=total_refs or None,
                )
                seen_monographs.update(row["index"] for row in parsed["monographs"])
                max_seen_index = max(max_seen_index, parsed["max_index"])
                pending_rows = [row for row in parsed["literature"] if row["index"] not in captured_by_index]
                if not pending_rows or not pending_rows[0]["safe"]:
                    break
                row = pending_rows[0]
                box = row["bounds"]
                tap_px = (box[0] + min((box[2] - box[0]) * .35, current.physical_size[0] * .35), (box[1] + box[3]) / 2)
                tap_logical = physical_to_logical(tap_px, current.physical_size, current.logical_size)
                self.engine.log("citation_detail_open", index=row["index"], title=row["title"])
                self.device.tap(tap_logical, expected_package=self.package)
                detail_obs = self._wait_citation_detail(current, row, tap_logical)
                obs_dir = self._save(f"citation-detail-{row['index']:02d}", detail_obs)
                item = {
                    "index": row["index"],
                    "list_title": row["title"],
                    "detail_title": _extract_citation_detail_title(detail_obs, row["title"]),
                    "observation": obs_dir,
                    "activity": detail_obs.activity,
                }
                captured_by_index[row["index"]] = item
                persist_progress()
                self.engine.log("citation_detail_captured", index=row["index"], title=row["title"],
                                detail_title=item["detail_title"], activity=detail_obs.activity, observation=obs_dir)
                current = self._return_from_citation_detail(detail_obs)
                current = self._refresh_answer_viewport(current)

            if expected_lit is not None and len(captured_by_index) >= expected_lit:
                break
            if expected_lit is None and seen_monographs and all(row["index"] in captured_by_index for row in parsed["literature"]):
                break
            if total_refs > 0 and max_seen_index >= total_refs and all(row["index"] in captured_by_index for row in parsed["literature"]):
                break
            self._swipe("down", .32)
            after = self._settled()
            if stable(self._region(current), self._region(after)):
                break
            current = after
            scrolled = True

        items = [captured_by_index[idx] for idx in sorted(captured_by_index)]
        if expected_lit is not None and expected_lit > 0 and len(items) < expected_lit:
            raise CaptureError(f"展开的医学文献应采集{expected_lit}篇详情截图，实际仅完成{len(items)}篇")
        if expected_lit is None and not items and not seen_monographs:
            raise CaptureError("已展开引用但未能识别或采集文献详情截图")

        if scrolled:
            for step in range(8):
                if direct_question_bubble(current, question, self.viewport):
                    break
                self._swipe("up", .35, step > 0)
                current = self._settled()
            current, _ = self._position(current, question)
            current = self._refresh_answer_viewport(current)

        details = {
            "required": bool(items),
            "present": bool(items),
            "complete": True,
            "header": canonical,
            "expected_count": expected_lit if expected_lit is not None else len(items),
            "skipped_monographs": len(seen_monographs),
            "items": items,
            "image_paths": [],
        }
        return current, details

    def _checkpoint_products(self, checkpoint):
        checkpoint["counter"] = self.counter
        self.engine.state["products_resume"] = checkpoint
        self.engine.save()

    def _drawer_ready(self, current):
        deadline = self._clock() + 20
        while self._clock() < deadline:
            state = self._drawer_state(current, request=True)
            if not state or not state["expanded"] or tuple(state["viewport"]) != tuple(self.viewport):
                raise CaptureError("药品抽屉全屏列表视口改变，停止采集")
            boxes = self._ready_images(current)
            if boxes:
                self.engine.resolve_capture_request("products.images_wait")
                self.engine.log("product_images_ready", regions=len(boxes))
                return current, boxes
            self.engine.log("product_images_wait", regions=0)
            # No detected photo is a layout failure; waiting cannot teach a new container.
            if not _product_image_boxes(current, self.viewport, canvas_cards=True):
                boxes = self._ready_images(current, request=True)
                return current, boxes
            self._sleep(.5)
            current = self._settled()
        self.engine._request("products.images_wait", "等待当前药品图片加载；保留原位置，不点击购买、不滚动，不改变图片就绪条件", current, (),
                             "药品图片未加载，等待超时，仅允许原位置检查后 resume", uncertain_action=True)

    def _products_entry_source(self, body):
        """Find the real entry in an observation that reproduces a saved body frame."""
        files, bounds = body.get("frame_files", []), body.get("frame_viewports", [])
        if len(files) != len(bounds):
            raise CaptureError("药品入口回溯缺少原始正文帧和视口")
        frames = []
        for file, frame in zip(files, bounds):
            path = Path(file).resolve()
            if path.parent != self.attempt:
                raise CaptureError("药品入口的正文帧不属于当前采集")
            frames.append((_image(path.read_bytes()), frame["viewport_physical"]))
        originals = sorted([*self.attempt.glob("*-scroll/observation.json"),
                            *self.attempt.glob("*-first-frame/observation.json"),
                            *self.attempt.glob("*-seam-recapture/observation.json")], reverse=True)
        for file in originals:
            original = load_observation(file.parent)
            if (original.package != self.package or (tuple(original.logical_size), tuple(original.physical_size)) != self.geometry
                    or tuple(answer_viewport(original)) != tuple(self.viewport)):
                continue
            entry = _visible_products_entry(original, self.viewport)
            if not entry:
                continue
            for frame, viewport in frames:
                candidate = _image(crop(original.png, viewport))
                if candidate.size == frame.size and candidate.tobytes() == frame.tobytes():
                    return {"observation": str(file.parent), "label": _label(entry), "bounds": list(entry["bounds"])}
        return None

    def _restore_products_entry(self, current, body, checkpoint):
        """Revisit a captured MiniApp entry before the terminal medicine scan."""
        if self.engine.state.get("entry") != "douyin-direct":
            return current
        if _visible_products_entry(current, self.viewport) and not checkpoint.get("entry_relocation"):
            return current
        # Never move an uncertain click's scene; perform() must first read-check it.
        if self.engine.state.get("inflight") or self.engine.state.get("exploration", {}).get("dispatched"):
            return current
        pending = self.engine.state.get("pending_request")
        request = read_json(Path(pending)) if pending else None
        if request and (request.get("step") != "products.open" or request.get("uncertain_action")
                        or request.get("uncertain_send")):
            return current
        navigation = checkpoint.get("entry_relocation")
        if navigation is None:
            source = self._products_entry_source(body)
            if source is None:
                return current
            saved_body = self.engine.state.get("body_resume", {})
            last_path = saved_body.get("last_observation")
            if (saved_body.get("attempt") != str(self.attempt) or not last_path
                    or Path(last_path).resolve().parent != self.attempt):
                raise CaptureError("药品入口回溯缺少本题已采末帧")
            last = load_observation(last_path)
            if not stable(last.png, current.png, bounds=self.viewport):
                raise CaptureError("当前现场与已采正文末帧不一致，不能寻找药品入口")
            navigation = {"source": source, "trace": [{"observation": self._save("products-entry-origin", current)}],
                          "swipe_pending": False, "complete": False}
            checkpoint["entry_relocation"] = navigation
            self._checkpoint_products(checkpoint)
            self.engine.log("products_entry_relocation_started", original_entry=source,
                            body_frames=len(body["frame_files"]), body_rescan=False)
        if tuple(answer_viewport(current)) != tuple(self.viewport):
            raise CaptureError("药品入口回溯的正文视口发生变化")
        prior = load_observation(navigation["trace"][-1]["observation"])
        if not stable(prior.png, current.png, bounds=self.viewport):
            if not navigation.get("swipe_pending"):
                raise CaptureError("药品入口回溯现场变化且没有本次滚动记录")
            seam = direct_scroll_seam(prior, current, self.viewport, "up")
            navigation["trace"].append({"observation": self._save("products-entry-backtrack", current), "seam": seam})
        navigation["swipe_pending"] = False
        self._checkpoint_products(checkpoint)
        while not _visible_products_entry(current, self.viewport):
            if len(navigation["trace"]) > len(body["frame_files"]) + 2:
                raise CaptureError("已采范围内未重新找到真实全部药品入口")
            navigation["swipe_pending"] = True
            self._checkpoint_products(checkpoint)
            self._swipe("up", .43)
            after = self._settled()
            if tuple(answer_viewport(after)) != tuple(self.viewport):
                raise CaptureError("药品入口回溯的正文视口发生变化")
            seam = direct_scroll_seam(current, after, self.viewport, "up")
            if seam["shift"] <= 0:
                raise CaptureError("尚未找到药品入口且正文没有继续回溯")
            navigation["trace"].append({"observation": self._save("products-entry-backtrack", after), "seam": seam})
            navigation["swipe_pending"] = False
            self._checkpoint_products(checkpoint)
            current = after
        entry = _visible_products_entry(current, self.viewport)
        # The real label plus continuous original pixels bind this target to the
        # already captured answer; medicine titles and shopping controls cannot qualify.
        canonical = lambda text: re.sub(r"[\s>›〉》»→⟩]+$", "", text)
        if canonical(_label(entry)) != canonical(navigation["source"]["label"]):
            raise CaptureError("回溯到的药品入口与原始正文记录不一致")
        navigation.update(complete=True, current_entry={"label": _label(entry), "bounds": list(entry["bounds"])})
        self._checkpoint_products(checkpoint)
        if pending and hasattr(self.engine, "_close_request"):
            self.engine._close_request(Path(pending), "superseded_by_verified_entry_relocation")
        self.engine.log("products_entry_relocated", original_entry=navigation["source"]["observation"],
                        scrolls=len(navigation["trace"]) - 1, body_frames=len(body["frame_files"]),
                        body_frames_unchanged=True, submission_replayed=False)
        return current

    def _drawer(self, current, body, checkpoint=None):
        checkpoint = checkpoint or {"question": body["question"], "package": self.package,
                                    "entry": self.engine.state.get("entry"), "index": self.engine.state.get("index"),
                                    "attempt": str(self.attempt), "body": body, "phase": "open",
                                    "frame_files": [], "seams": []}
        self._checkpoint_products(checkpoint)
        first_settled = False
        if checkpoint["phase"] == "open":
            if self._drawer_state(current) is None:
                current = self._restore_products_entry(current, body, checkpoint)
            self.engine.log("products_entry_found", terminal_sequence=True)
            target = {"kind": "ocr", "pattern": PRODUCT_ENTRY}
            title = {"kind": "ocr", "pattern": PRODUCT_TITLE}
            opening = [False]
            pending_layout = self.engine.state.get("pending_request")
            pending_layout = bool(pending_layout and read_json(Path(pending_layout)).get("step") == "layout.product_list")
            def opened_check(item):
                heading = any(re.fullmatch(PRODUCT_TITLE, _label(line)) and line["bounds"][1] < item.physical_size[1] * .70 for line in _physical_labels(item))
                heading = heading or bool(role_nodes(item, "products_header"))
                return item.package == self.package and self._drawer_state(item, request=heading and (opening[0] or pending_layout)) is not None
            current = self.engine.perform("products.open", "打开回答尾部的完整参考药品列表，不点击药品购买或详情", self.package,
                                          opened_check,
                                          defaults=({"actions": [{"type": "tap", "target": target}]},
                                                    {"actions": [{"type": "tap", "target": title}]}),
                                          hints=(PRODUCT_ENTRY, PRODUCT_TITLE), risk="toggle", before_action=lambda: opening.__setitem__(0, True))
            current = self._settled(current)
            opened = self._drawer_state(current)
            if not opened or not opened["first_item"]:
                raise CaptureError("药品抽屉已打开但不能确认初始首项，禁止滚动或反滑恢复")
            checkpoint.update(phase="expand", first_item=opened["first_item"])
            self._checkpoint_products(checkpoint)
            self.engine.log("products_drawer_opened", first_item_confirmed=True)
        if checkpoint["phase"] == "expand":
            def expanded(item):
                state = self._drawer_state(item) if item.package == self.package else None
                return bool(state and state["expanded"] and state["first_item"] == checkpoint["first_item"])
            current = self.engine.perform("products.expand", "仅从药品标题或拖拽柄向上展开到全屏；不能拖动药品内容，必须保留打开时的首项", self.package,
                                          expanded, defaults=({"actions": [{"type": "swipe", "direction": "down", "distance_fraction": .65,
                                                                            "target": {"kind": "ocr", "pattern": PRODUCT_TITLE}}]},),
                                          hints=(PRODUCT_TITLE,))
            current = self._settled(current)
            state = self._drawer_state(current)
            if not expanded(current):
                raise CaptureError("药品抽屉未全屏或展开丢失首项，不能从中途采集")
            self.viewport = tuple(state["viewport"])
            current, boxes = self._drawer_ready(self._settled(current))
            state = self._drawer_state(current)
            first_settled = True
            if state["first_item"] != checkpoint["first_item"] or boxes[0][1] > state["first_item_bounds"][3] + current.physical_size[1] * .04:
                raise CaptureError("全屏药品列表首项图文未完整确认")
            first = crop(current.png, (self.viewport[0], state["header"]["bounds"][1], self.viewport[2], self.viewport[3]))
            path = self.attempt / "product-frame-001.png"
            path.write_bytes(first)
            self._save("products-fullscreen-first", current)
            checkpoint.update(phase="list", viewport=list(self.viewport), frame_files=[str(path)],
                              image_regions_checked=len(boxes), moved=False, end_probes=0)
            self._checkpoint_products(checkpoint)
            self.engine.log("products_drawer_expanded", viewport=list(self.viewport), first_item_confirmed=True)
        self.viewport = tuple(checkpoint["viewport"])
        frames = [Path(path).read_bytes() for path in checkpoint["frame_files"]]
        seams = checkpoint["seams"]
        if not first_settled:
            current, boxes = self._drawer_ready(self._settled(current))
        carousels = ProductCarousels(self, checkpoint)
        # A horizontal swipe may have completed before its checkpoint was saved.
        # Finish/restore that row before comparing vertical list pixels.
        current = carousels.capture_visible(current)

        def append(observation, distance=None, started=None):
            frame = self._region(observation)
            try:
                seam = match_product_overlap(frames[-1], frame, expected_shift=distance)
            except CaptureError:
                self.engine.log("product_seam_retry", frame=len(frames) + 1)
                observation, _ = self._drawer_ready(self._settled(observation))
                self._save("product-seam-recapture", observation)
                frame = self._region(observation)
                seam = match_product_overlap(frames[-1], frame, expected_shift=distance)
            if not seam["verified"] or seam["shift"] <= 0:
                raise CaptureError("药品接缝未证明连续新增内容，不能跳过或堆叠视口")
            frames.append(frame)
            seams.append(seam)
            path = self.attempt / f"product-frame-{len(frames):03d}.png"
            path.write_bytes(frame)
            checkpoint["frame_files"].append(str(path))
            checkpoint.update(moved=True, end_probes=0)
            self._checkpoint_products(checkpoint)
            self.engine.log("product_frame_captured", frame=len(frames), seam=seam,
                            seconds=round(self._clock() - started, 3) if started is not None else None)
            return observation

        # A resumed list may have completed its last swipe before checkpointing.
        # Verify that displacement before issuing another gesture.
        previous_list = crop(frames[-1], (0, image_size(frames[-1])[1] - (self.viewport[3] - self.viewport[1]), image_size(frames[-1])[0], image_size(frames[-1])[1]))
        if not stable(previous_list, self._region(current)):
            current = append(current)
        unchanged = 0
        conservative = self.engine.state.get("entry") == "douyin-direct"
        had_seam_retry = False
        while unchanged < (2 if checkpoint["moved"] else 3):
            started = self._clock()
            if conservative:
                has_carousel = bool(product_carousels(current, self.viewport, include_static=False))
                if has_carousel or had_seam_retry:
                    # Tall horizontal medicine cards must enter one viewport in full;
                    # a slow, short drag also avoids fling-dependent missing frames.
                    distance = self._swipe("down", .35, alternate=unchanged > 0, duration=.9)
                else:
                    distance = self._swipe("down", .48, alternate=unchanged > 0, duration=.55)
            else:
                distance = self._swipe("down", self._stride(seams), alternate=unchanged > 0)
            after, boxes = self._drawer_ready(self._settled())
            self._save("products-scroll", after)
            checkpoint["image_regions_checked"] += len(boxes)
            if stable(self._region(current), self._region(after)):
                unchanged += 1
                checkpoint["end_probes"] = unchanged
                self._checkpoint_products(checkpoint)
                self.engine.log("products_end_probe", unchanged=unchanged, moved=checkpoint["moved"])
                current = after
                continue
            unchanged = 0
            try:
                current = append(after, distance, started)
            except CaptureError:
                had_seam_retry = True
                raise
            current = carousels.capture_visible(current)
        current = carousels.capture_visible(current, final=True)
        self.engine.log("products_capture_complete", frames=len(frames), end_probes=unchanged, terminal_sequence=True)
        return frames, {"present": True, "mode": "drawer", "complete": True, "first_item_confirmed": True,
                        "first_item_confirmation_method": "header_expansion_preserved_initial_item", "end_confirmed": True,
                        "images_ready": True, "image_regions_checked": checkpoint["image_regions_checked"],
                        "seams_verified": True, "seams": seams, "frame_files": checkpoint["frame_files"],
                        "frame_count": len(frames), "end_probes": unchanged, "terminal_sequence": True,
                        "restore_required": False, "post_scan_swipes": 0, "viewport_physical": list(self.viewport),
                        "panel_top_physical": self._drawer_state(current)["panel_top"],
                        "horizontal": checkpoint["horizontal"],
                        "vertical_scroll_mode": "short_slow" if conservative else "adaptive"}

    def _body(self, current, question, checkpoint=None):
        resumed = checkpoint is not None
        if not resumed:
            prepared = self.engine.state.get("capture_preparation")
            identity = [question, self.package, self.engine.state.get("entry"), self.engine.state.get("index")]
            reuse = False
            if prepared and prepared["identity"] == identity:
                prior = load_observation(prepared["observation"])
                current_question = (direct_question_bubble(current, question, self.viewport)
                                    if self.engine.state.get("entry") == "douyin-direct"
                                    else question_bubble(current, question, self.viewport))
                reuse = (prepared.get("stage", "citations") == "citations"
                         and prior.logical_size == current.logical_size and prior.physical_size == current.physical_size
                         and stable(prior.png, current.png, bounds=self.viewport) and current_question is not None)
            if reuse:
                self.engine.log("capture_preparation_resumed", generation_rescan=False, top_rescan=False)
            else:
                pending = self.engine.state.get("pending_request")
                question_pending = bool(pending and read_json(Path(pending)).get("step") == "capture.question_top")
                question_pending = question_pending or self.engine.state.get("exploration", {}).get("step") == "capture.question_top"
                reused_direct = False
                if question_pending:
                    if (not prepared or prepared.get("stage") != "question" or prepared["identity"] != identity
                            or (tuple(prior.logical_size), tuple(prior.physical_size)) != self.geometry):
                        raise CaptureError("原题首屏恢复缺少本题生成确认断点，不能跳过完成检查")
                    self.engine.log("question_goal_resumed", generation_rescan=False)
                else:
                    if self.engine.state.get("entry") == "douyin-direct":
                        from .platforms import douyin_direct
                        task = next((item for item in self.engine.state.get("tasks", [])
                                     if (item.get("entry"), item.get("index"), item.get("text"))
                                     == ("douyin-direct", self.engine.state.get("index"), question)), None)
                        proof = douyin_direct.submission_evidence(task, self.engine.run_dir) if task else None
                        if proof and proof.get("question_observation") and proof.get("latest_tail"):
                            try:
                                confirmed_q = load_observation(proof["question_observation"])
                                if (confirmed_q.package == current.package
                                        and (tuple(confirmed_q.logical_size), tuple(confirmed_q.physical_size)) == self.geometry
                                        and stable(confirmed_q.png, current.png, bounds=self.viewport)
                                        and direct_question_bubble(current, question, self.viewport) is not None):
                                    self.engine.log("direct_submission_preparation_reused",
                                                    generation_rescan=False, top_rescan=False,
                                                    latest_tail=proof["latest_tail"])
                                    current, _ = self._position(current, question)
                                    reused_direct = True
                            except (OSError, KeyError, TypeError, ValueError):
                                reused_direct = False
                    if not reused_direct:
                        self.engine.state["capture_objective"] = "capture.generation"
                        current = self._complete(current, question)
                        current = self._refresh_answer_viewport(current)
                        self.engine.state["capture_preparation"] = {
                            "identity": identity, "stage": "question", "observation": self._save("generation-confirmed", current),
                            "viewport_physical": list(self.viewport)}
                        self.engine.save()
                if not reused_direct:
                    self.engine.state["capture_objective"] = "capture.question_top"
                    self._question_method_used = False
                    current, _ = self._question_start(current, question)
                    if self._question_method_used and not self.engine.state["capture_preparation"].get("question_confirmed"):
                        # A route may have left a different answer. Its old generation
                        # evidence cannot certify the answer whose exact question we just found.
                        self.engine.state["capture_preparation"] = {
                            "identity": identity, "stage": "generation", "observation": self._save("question-verified", current)}
                        self.engine.state["capture_objective"] = "capture.generation"
                        self.engine.save()
                        current = self._complete(current, question)
                        current = self._refresh_answer_viewport(current)
                        self.engine.state["capture_preparation"] = {
                            "identity": identity, "stage": "question", "question_confirmed": True,
                            "observation": self._save("generation-confirmed", current), "viewport_physical": list(self.viewport)}
                        self.engine.state["capture_objective"] = "capture.question_top"
                        self.engine.save()
                        current, _ = self._question_start(current, question)
            saved_cd = prepared.get("citation_details") if isinstance(prepared, dict) and prepared.get("identity") == identity else None
            self.engine.state["capture_preparation"] = {
                "identity": identity, "stage": "citations", "observation": self._save("prepared-top", current),
                **({"citation_details": saved_cd} if saved_cd else {}),
            }
            self.engine.save()
            teaching = getattr(self.engine, "teach", set()) & {"page.perception", "page.perception." + self.engine.state.get("entry", "")}
            if teaching:
                current = self.engine.perception(current, "首次学习：查看整屏并绑定当前可见的真实页面角色，采集目标保持不变")
                self.engine.teach.difference_update(teaching)
                self.engine.save()
            self.engine.state["capture_objective"] = "citation.expand"
            current, citations = self._citations(current)
            current = self._refresh_answer_viewport(current)
            current, target = self._position(current, question)
            if citations["present"] and not citation_state(current, self.viewport, citations["header"]["canonical"])["expanded"]:
                raise CaptureError("首帧位置调整后不能确认引用仍完整展开")
            self.engine.state["capture_preparation"] = {
                "identity": identity, "stage": "citations",
                "observation": self._save("citations-expanded", current),
                **({"citation_details": saved_cd} if saved_cd else {}),
            }
            self.engine.save()
            current, citation_details = self._capture_citation_details(current, question, citations)
            if citation_details and citation_details.get("present"):
                current, target = self._position(current, question)
                if citations["present"] and not citation_state(current, self.viewport, citations["header"]["canonical"])["expanded"]:
                    raise CaptureError("采集参考文献详情返回后不能确认引用仍完整展开")
            first_viewport, first_info = capture_viewport(current, self.viewport)
            first_bounds = (first_viewport[0], max(first_viewport[1], target["bounds"][1] - current.physical_size[1] * .006), first_viewport[2], first_viewport[3])
            frame_file = self.attempt / "frame-001.png"
            frame_file.write_bytes(crop(current.png, first_bounds))
            checkpoint = {"question": question, "package": self.package, "entry": self.engine.state.get("entry"), "index": self.engine.state.get("index"),
                          "attempt": str(self.attempt), "frame_files": [str(frame_file)], "seams": [], "citations": citations, "citation_details": citation_details, "target": target,
                          "frame_viewports": [{**first_info, "viewport_physical": list(first_bounds)}], "products": product_state(current, self.viewport),
                          "geometry": {"logical_size": list(self.geometry[0]), "physical_size": list(self.geometry[1]), "viewport_physical": list(self.viewport)},
                          "last_observation": self._save("first-frame", current), "counter": self.counter}
            self.engine.state["body_resume"] = checkpoint
            self.engine.state.pop("capture_preparation", None)
            self.engine.save()
        frames = [Path(path).read_bytes() for path in checkpoint["frame_files"]]
        self.engine.state["capture_objective"] = "capture.body"
        frame_files, seams, frame_viewports = checkpoint["frame_files"], checkpoint["seams"], checkpoint["frame_viewports"]
        citations, target, products = checkpoint["citations"], checkpoint["target"], checkpoint["products"]
        citation_details = checkpoint.get("citation_details")
        resume_after = self._settled(current) if resumed else None
        if resumed:
            current = load_observation(checkpoint["last_observation"])
            self.engine.log("answer_body_resumed", verified_frames=len(frames), top_rescan=False, generation_rescan=False)
        deadline, unchanged = self._clock() + self.timeout_seconds, 0
        def inspect_products(observation):
            nonlocal products
            candidate = product_state(observation, self.viewport, pending=products.get("pending", False), image_evidence=products.get("image_regions_checked", 0) > 0,
                                      text_only_evidence=products.get("text_only", False))
            if candidate["present"]:
                if not candidate.get("images_ready", True):
                    raise CaptureError("内联参考药品图片尚未确认加载，已保留当前位置且不继续滚动")
                candidate["image_regions_checked"] = candidate.get("image_regions_checked", 0) + products.get("image_regions_checked", 0)
                products = candidate
                checkpoint["products"] = products
                self.engine.save()
        while unchanged < 2:
            started = self._clock()
            if self._clock() >= deadline:
                raise CaptureError("正文采集超时且未证明到底，已保留已有帧")
            inspect_products(current)
            if resume_after is not None:
                after, resume_after, distance = resume_after, None, None
            else:
                distance = self._swipe("down", self._stride(seams), alternate=unchanged > 0)
                after = self._settled()
            observed_at = self._save("scroll", after)
            current_bounds, _ = capture_viewport(current, self.viewport)
            after_bounds, frame_info = capture_viewport(after, self.viewport)
            if current_bounds == after_bounds and stable(crop(current.png, current_bounds), crop(after.png, after_bounds)):
                # A read-only resume comparison is not an end-of-scroll probe.
                unchanged += int(distance is not None)
                current = after
                continue
            unchanged = 0
            frame = crop(after.png, after_bounds)
            try:
                seam = match_overlap(frames[-1], frame, expected_shift=distance)
            except CaptureError:
                after = self._settled(after)
                observed_at = self._save("seam-recapture", after)
                after_bounds, frame_info = capture_viewport(after, self.viewport)
                frame = crop(after.png, after_bounds)
                seam = match_overlap(frames[-1], frame, expected_shift=distance)
            if not seam["verified"] or seam["shift"] < 0 or image_size(frame)[1] <= seam["overlap"]:
                raise CaptureError("正文接缝未证明连续新增内容")
            frames.append(frame)
            frame_viewports.append(frame_info)
            seams.append(seam)
            frame_file = self.attempt / f"frame-{len(frames):03d}.png"
            frame_file.write_bytes(frame)
            frame_files.append(str(frame_file))
            checkpoint.update(last_observation=observed_at, counter=self.counter)
            self.engine.save()
            self.engine.log("answer_frame_captured", frame=len(frames), seam=seam, seconds=round(self._clock() - started, 3), **frame_info)
            current = after
        inspect_products(current)
        if products["mode"] != "drawer":
            if products["present"] and not products["complete"]:
                raise CaptureError("末屏存在未完整采集或未确认首项的参考药品卡片")
            products.update(end_confirmed=True, seams_verified=True)
        return current, {"question": question, "question_confirmed": True, "question_confirmation_method": target["method"],
                         "question_visible": target.get("question_visible", True), "answer_start": target,
                         "citation_confirmation_method": citations.get("method", "builtin"),
                         "citations_checked": True, "citations_found": citations["present"], "citations_expanded": citations["expanded"],
                         **({"citation_details": citation_details} if citation_details is not None else {}),
                         "completion_confirmed": True, "answer_content_confirmed": True, "end_confirmed": True, "frame_count": len(frames), "seams": seams,
                         "occlusion_checked": True, "frame_viewports": frame_viewports,
                         "frame_files": frame_files, "products": products,
                         "geometry": {"logical_size": list(self.geometry[0]), "physical_size": list(self.geometry[1]), "viewport_physical": list(self.viewport)},
                         "diagnostics": str(self.attempt)}

    def _refresh_answer_viewport(self, observation):
        observation = self._recover_network(observation)
        observation = self._dismiss_guide(observation)
        viewport = self._answer_viewport(observation)
        if tuple(viewport) != tuple(self.viewport):
            self.engine.log("answer_viewport_refreshed", before=list(self.viewport), after=list(viewport))
            self.viewport = viewport
        return observation

    def _recover_network(self, observation):
        pending = getattr(self.engine, "state", {}).get("pending_request")
        retry_pending = bool(pending and read_json(Path(pending)).get("step") == "network.retry")
        if not network_error_visible(observation) and not retry_pending:
            return observation
        self._save("network-error", observation)
        def loaded(item):
            if item.package != self.package or network_error_visible(item):
                return False
            try:
                answer_viewport(item)
                return _answer_content_visible(item, answer_viewport(item), getattr(self, "question", ""))
            except CaptureError:
                return False
        current = self.engine.perform("network.retry", "重试加载当前已发送问题的回答，不输入或重发问题", self.package,
                                      loaded, defaults=({"actions": [{"type": "tap", "target": {"kind": "ocr", "pattern": r"^重试$"}}]},),
                                      hints=(r"^重试$",), risk="toggle")
        self.engine.log("network_retry_complete", answer_viewport_verified=True)
        return current

    def run(self, question: str, package: str, delivery: Path, debug: Path) -> dict:
        self.package, self.question, self.counter, self._last = package, question, 0, None
        if hasattr(self, "geometry"):
            del self.geometry
        checkpoint = self.engine.state.get("products_resume")
        body_checkpoint = self.engine.state.get("body_resume") if not checkpoint else None
        self.attempt = Path(debug).resolve() / ("capture-" + uuid4().hex[:12])
        existing = checkpoint or body_checkpoint
        if existing:
            identity = (question, package, self.engine.state.get("entry"), self.engine.state.get("index"))
            if identity != tuple(existing.get(key) for key in ("question", "package", "entry", "index")):
                raise CaptureError("药品恢复断点不属于当前题目或入口，已停止")
            self.attempt = Path(existing["attempt"]).resolve()
            if not self.attempt.is_relative_to(Path(debug).resolve()):
                raise CaptureError("药品断点不属于当前调试目录")
            self.counter = existing["counter"]
            geometry = checkpoint["body"]["geometry"] if checkpoint else body_checkpoint["geometry"]
            self.geometry = tuple(geometry["logical_size"]), tuple(geometry["physical_size"])
        self.attempt.mkdir(parents=True, exist_ok=True)
        try:
            direct_proof = None
            if self.engine.state.get("entry") == "douyin-direct":
                from .platforms import douyin_direct
                task = next((item for item in self.engine.state.get("tasks", [])
                             if (item.get("entry"), item.get("index"), item.get("text"))
                             == ("douyin-direct", self.engine.state.get("index"), question)), None)
                direct_proof = douyin_direct.submission_evidence(task, self.engine.run_dir) if task else None
                expected_debug = Path(self.engine.run_dir).resolve() / "调试产物" / "douyin-direct" / f"{self.engine.state['index']:03d}"
                if (direct_proof is None or package != douyin_direct.PACKAGE
                        or Path(debug).resolve() != expected_debug):
                    raise CaptureError("直接入口缺少本题原始发送身份证据，不能开始或恢复采集")
            self.engine.state["capture_objective"] = "capture.answer_page"
            question_pending = hasattr(self.engine, "goal_pending") and self.engine.goal_pending("capture.question_top")
            if question_pending and not existing:
                prepared = self.engine.state.get("capture_preparation", {})
                identity = [question, package, self.engine.state.get("entry"), self.engine.state.get("index")]
                if prepared.get("identity") != identity or prepared.get("stage") != "question":
                    raise CaptureError("原题导航恢复缺少本题采集准备证据")
                prior = load_observation(prepared["observation"])
                current = self.engine.observe()
                validate_geometry(current.logical_size, current.physical_size)
                self.geometry = tuple(prior.logical_size), tuple(prior.physical_size)
                if current.package != package or prior.package != package or (tuple(current.logical_size), tuple(current.physical_size)) != self.geometry:
                    raise CaptureError("原题导航恢复的应用或窗口尺寸不一致")
                self.viewport = tuple(prepared["viewport_physical"])
                self._last = current
            else:
                current = self._observe(ocr=True)
            if not hasattr(self.engine, "review_capture"):
                self.engine.resolve_capture_request("capture.review")
            if checkpoint:
                metadata = checkpoint["body"]
                self.viewport = tuple(metadata["geometry"]["viewport_physical"])
                self.engine.log("products_terminal_resumed", phase=checkpoint["phase"], body_rescan=False)
            elif body_checkpoint:
                self.viewport = tuple(body_checkpoint["geometry"]["viewport_physical"])
                current, metadata = self._body(current, question, body_checkpoint)
            elif question_pending:
                current, metadata = self._body(current, question)
            else:
                current = self._close_existing_drawer(current)
                if answer_page_ready(current):
                    if hasattr(self.engine, "resolve_capture_request"):
                        self.engine.resolve_capture_request("capture.answer_page")
                else:
                    current = self.engine.perform("capture.answer_page", "恢复当前已有回答的完整页面，关闭侧栏或中间页；不切换题目、不聚焦输入、不新建会话", self.package,
                                                  answer_page_ready, defaults=())
                current = self._recover_network(current)
                self.geometry = tuple(current.logical_size), tuple(current.physical_size)
                self.viewport = self._answer_viewport(current)
                self._save("initial", current)
                pending_file = self.engine.state.get("pending_request")
                if pending_file:
                    request = read_json(Path(pending_file))
                    if request.get("step") == "page.perception":
                        current = self.engine.perception(current, request["reason"])
                        self.engine.teach.discard("page.perception")
                        self.engine.teach.discard("page.perception." + self.engine.state.get("entry", ""))
                        self.engine.save()
                    if request.get("step") in ("citation.expand", "overlay.dismiss"):
                        if (request.get("package") != package or request.get("entry") != self.engine.state.get("entry")
                                or request.get("question_index") != self.engine.state.get("index")):
                            raise CaptureError("修复断点不属于当前题目或入口")
                        if request["step"] == "overlay.dismiss":
                            current = self._dismiss_guide(current, pending=True)
                        else:
                            current, _ = self._citations(current, pending=True)
                current, metadata = self._body(current, question)
            if direct_proof is not None:
                metadata["direct_submission"] = direct_proof
                first_frame = Path(metadata["frame_files"][0]).resolve()
                first_scenes = list(first_frame.parent.glob("*-first-frame/observation.json"))
                if len(first_scenes) != 1:
                    raise CaptureError("直接入口缺少唯一原始首帧观察，不能绑定本题身份")
                metadata["direct_first_frame"] = str(first_scenes[0].parent.resolve())
                verify_direct_capture_identity(metadata, debug)
            products_frames = []
            if metadata["products"]["mode"] == "drawer":
                self.engine.state["capture_objective"] = "capture.products"
                products_frames, metadata["products"] = self._drawer(current, metadata, checkpoint)
            self.engine.state["capture_objective"] = "capture.delivery"
            frames = [Path(path).read_bytes() for path in metadata["frame_files"]]
            output = [("回答", compose(frames, [item["overlap"] for item in metadata["seams"]]))]
            if products_frames:
                output.append(("回答_参考药品", compose(products_frames, [item["overlap"] for item in metadata["products"]["seams"]])))
                horizontal_files = [file for row in metadata["products"]["horizontal"]["rows"]
                                    if len(row["frame_files"]) > 1 for file in row["frame_files"]]
                if horizontal_files:
                    output.append(("回答_参考药品_横向", [Path(file).read_bytes() for file in horizontal_files]))
            delivery = Path(delivery).resolve()
            delivery.mkdir(parents=True, exist_ok=True)
            paths, product_paths, horizontal_paths = [], [], []
            for prefix, parts in output:
                for index, part in enumerate(parts, 1):
                    destination = delivery / f"{prefix}_{index:03d}.png"
                    destination.write_bytes(part)
                    paths.append(str(destination))
                    if prefix == "回答_参考药品":
                        product_paths.append(str(destination))
                    elif prefix == "回答_参考药品_横向":
                        horizontal_paths.append(str(destination))
            metadata["image_paths"] = paths
            citation_details = metadata.get("citation_details")
            if isinstance(citation_details, dict) and citation_details.get("items"):
                citation_detail_paths = []
                for seq, item in enumerate(citation_details["items"], 1):
                    destination = delivery / f"回答_参考文献_{seq:03d}.png"
                    destination.write_bytes((Path(item["observation"]) / "screen.png").read_bytes())
                    item["image_path"] = str(destination)
                    citation_detail_paths.append(str(destination))
                    paths.append(str(destination))
                citation_details["image_paths"] = citation_detail_paths
            source = self._search_source(question)
            if source:
                destination = delivery / "搜索结果.png"
                destination.write_bytes((source / "screen.png").read_bytes())
                paths.append(str(destination))
                metadata["search_result"] = {"observation": str(source), "image_path": str(destination)}
            if product_paths:
                metadata["products"]["image_paths"] = product_paths
                metadata["products"]["horizontal"]["image_paths"] = horizontal_paths
            verify_capture_images(metadata, debug, delivery)
            write_json(self.attempt / "capture.json", metadata)
            self.engine.state.pop("products_resume", None)
            self.engine.state.pop("body_resume", None)
            self.engine.state.pop("capture_objective", None)
            self.engine.commit_layouts()
            self.engine.save()
            self.engine.log("answer_capture_complete", frames=len(frames), seams=len(metadata["seams"]), images=paths)
            return metadata
        except Exception as error:
            try:
                if self._last is not None:
                    self._save("failure", self._last)
                write_json(self.attempt / "failure.json", {"error": str(error), "type": type(error).__name__})
            except Exception as diagnostic_error:
                self.engine.log("capture_diagnostic_failed", error=str(diagnostic_error))
            self.engine.log("answer_capture_failed", error=str(error), diagnostics=str(self.attempt))
            if isinstance(error, CaptureError) and self._last is not None:
                uncertain = bool(self.engine.state.get("inflight") or self.engine.state.get("exploration", {}).get("dispatched"))
                self.engine._request("capture.review", "根据缺失证据恢复本题采集；可重新识别页面角色、等待后resume或使用有记录的recover，完整性证据仍须通过原检查", self._last, (),
                                     str(error), uncertain_action=uncertain)
            raise


def verify_direct_capture_identity(result, debug):
    """Independently bind direct-entry pixels to this run's original send proof."""
    from .platforms import douyin_direct
    debug = Path(debug).resolve()
    if debug.parent.name != douyin_direct.ID and result.get("entry") != douyin_direct.ID:
        return
    if debug.parent.name != douyin_direct.ID or debug.parent.parent.name != "调试产物":
        raise CaptureError("直接入口的原始证据目录不属于本题")
    try:
        index = int(debug.name)
        run_dir = debug.parents[2]
        state = read_json(run_dir / "调试产物" / "run.json")
        task = next((item for item in state.get("tasks", [])
                     if (item.get("entry"), item.get("index"), item.get("text"))
                     == (douyin_direct.ID, index, result.get("question"))), None)
        proof = douyin_direct.submission_evidence(task, run_dir) if task else None
        if proof is None or result.get("direct_submission") != proof:
            raise CaptureError("直接入口的交付身份与本题原始发送证据不一致")
        scene = Path(result["direct_first_frame"]).resolve()
        first_file = Path(result["frame_files"][0]).resolve()
        if (not scene.is_relative_to(debug) or not scene.name.endswith("-first-frame")
                or scene.parent != first_file.parent or not first_file.is_relative_to(debug)):
            raise CaptureError("直接入口首帧证据路径不属于本题")
        first = load_observation(scene)
        question = load_observation(proof["question_observation"])
        if (first.package != douyin_direct.PACKAGE or first.activity != question.activity
                or first.logical_size != question.logical_size or first.physical_size != question.physical_size
                or not douyin_direct.answer_ready(first)):
            raise CaptureError("直接入口首帧的应用或窗口与发送身份证据不一致")
        target = direct_question_bubble(first, result["question"], answer_viewport(first))
        original = direct_question_bubble(question, result["question"], answer_viewport(question))
        if not target or not original or not stable(crop(first.png, target["bounds"]), crop(question.png, original["bounds"])):
            raise CaptureError("直接入口首帧没有保留已验证的最新用户原题")
        bounds = result["frame_viewports"][0]["viewport_physical"]
        if (not bounds[0] <= target["bounds"][0] < target["bounds"][2] <= bounds[2]
                or not bounds[1] <= target["bounds"][1] < target["bounds"][3] <= bounds[3]
                or _image(crop(first.png, bounds)).tobytes() != _image(first_file.read_bytes()).tobytes()
                or image_size(crop(first.png, bounds)) != image_size(first_file.read_bytes())):
            raise CaptureError("直接入口首帧 PNG 与原始问题观察不一致")
    except (OSError, KeyError, TypeError, IndexError, StopIteration) as error:
        raise CaptureError("直接入口的发送或首帧原始证据缺失，不能只凭确认标志验收") from error


def verify_capture_images(result, debug, delivery):
    """Rebuild delivery pixels from this question's saved frames and seams."""
    debug, delivery = Path(debug).resolve(), Path(delivery).resolve()
    verify_direct_capture_identity(result, debug)
    paths = result.get("image_paths")
    if (not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths)):
        raise CaptureError("正式图片路径缺失或重复")
    files_on_disk = {path.resolve() for path in delivery.rglob("*") if path.is_file() and path.name != ".DS_Store"}
    if files_on_disk != {Path(path).resolve() for path in paths}:
        raise CaptureError("交付目录文件与本题图片清单不一致")
    products = result.get("products", {})
    if not isinstance(products, dict):
        raise CaptureError("药品记录格式无效")
    drawer = products.get("present") and products.get("mode") == "drawer"
    product_paths = products.get("image_paths", []) if drawer else []
    if (not isinstance(product_paths, list) or any(not isinstance(path, str) for path in product_paths)
            or len(set(product_paths)) != len(product_paths) or not set(product_paths).issubset(paths)):
        raise CaptureError("药品交付图片路径无效")
    horizontal = products.get("horizontal")
    horizontal_paths = []
    if horizontal is not None:
        if not drawer or not isinstance(horizontal, dict):
            raise CaptureError("横向药品记录格式无效")
        verify_carousels(horizontal, debug, delivery)
        horizontal_paths = horizontal.get("image_paths", [])
        if not set(horizontal_paths).issubset(paths) or set(horizontal_paths) & set(product_paths):
            raise CaptureError("横向药品图片清单不属于本题或与纵向重复")
    source = result.get("search_result")
    source_paths = []
    if source:
        from .platforms import PLATFORMS
        directory = Path(source["observation"]).resolve()
        output = Path(source["image_path"]).resolve()
        if directory != debug / "entry-source" or output != delivery / "搜索结果.png" or str(output) not in paths:
            raise CaptureError("搜索入口证据路径无效")
        observation = load_observation(directory)
        platform = next((p for p in PLATFORMS.values() if p.PACKAGE == observation.package and p.REENTER_SUPPORTED), None)
        if (platform is None or not platform.results_loaded(observation, result["question"])
                or not (platform.brand_entry_defaults(observation) or platform.platform_entry_visible(observation))
                or output.read_bytes() != observation.png):
            raise CaptureError("搜索原题、入口或交付截图与原始观察不符")
        source_paths.append(str(output))
    citation_details = result.get("citation_details")
    citation_detail_paths = []
    if (debug.parent.name == "douyin-direct" or result.get("entry") == "douyin-direct") and result.get("citations_found"):
        if not isinstance(citation_details, dict) or citation_details.get("complete") is not True:
            raise CaptureError("抖音小程序入口缺少参考文献详情验证证据")
    if citation_details is not None:
        if not isinstance(citation_details, dict) or not isinstance(citation_details.get("present"), bool) or citation_details.get("complete") is not True:
            raise CaptureError("参考文献详情记录格式无效或未完成")
        if citation_details.get("present"):
            items = citation_details.get("items")
            cd_paths = citation_details.get("image_paths")
            if (not isinstance(items, list) or not items or not isinstance(cd_paths, list)
                    or len(items) != len(cd_paths) or len(set(cd_paths)) != len(cd_paths)):
                raise CaptureError("参考文献详情图片清单不完整或重复")
            expected_count = citation_details.get("expected_count", 0)
            if type(expected_count) is not int or (expected_count > 0 and len(items) < expected_count):
                raise CaptureError("参考文献详情数量少于引用标题声明篇数")
            if not set(cd_paths).issubset(paths) or set(cd_paths) & set(product_paths + horizontal_paths + source_paths):
                raise CaptureError("参考文献详情图片清单不属于本题或与其他图片重复")
            expected_pkg = "com.ss.android.ugc.aweme" if (debug.parent.name == "douyin-direct" or result.get("entry") == "douyin-direct") else None
            for seq, (item, out_path) in enumerate(zip(items, cd_paths), 1):
                if not isinstance(item, dict) or not isinstance(item.get("observation"), str):
                    raise CaptureError("参考文献详情观察路径缺失")
                obs_dir = Path(item["observation"]).resolve()
                output = Path(out_path).resolve()
                if (not obs_dir.is_relative_to(debug) or output != delivery / f"回答_参考文献_{seq:03d}.png"
                        or item.get("image_path") != str(output)):
                    raise CaptureError("参考文献详情证据或交付路径无效")
                detail_obs = load_observation(obs_dir)
                if output.read_bytes() != detail_obs.png:
                    raise CaptureError("参考文献详情交付截图与原始观察不一致")
                if not _citation_detail_valid(detail_obs, item.get("list_title", ""), expected_pkg or detail_obs.package):
                    raise CaptureError("参考文献详情原始观察未通过有效内容检查")
            citation_detail_paths = list(cd_paths)
    if result.get("question_visible") is False:
        target = result.get("answer_start", {})
        evidence = target.get("top_observations", [])
        if (not source or target.get("method") != "search_entry_and_verified_answer_top"
                or target.get("source_observation") != source["observation"] or len(evidence) != 3):
            raise CaptureError("原题不可见时缺少搜索对应及回答顶部证据")
        observations = []
        for path in evidence:
            if not Path(path).resolve().is_relative_to(debug):
                raise CaptureError("回答顶部证据不属于本题")
            item = load_observation(path)
            if item.package != observation.package:
                raise CaptureError("回答顶部与搜索入口应用不符")
            observations.append(item)
        viewport = result["geometry"]["viewport_physical"]
        if any(not stable(a.png, b.png, bounds=viewport) for a, b in zip(observations, observations[1:])):
            raise CaptureError("回答顶部尚未稳定")
        first = crop(observations[-1].png, result["frame_viewports"][0]["viewport_physical"])
        if _image(first).tobytes() != _image(Path(result["frame_files"][0]).read_bytes()).tobytes():
            raise CaptureError("首帧不对应已验证的回答顶部")
    groups = [(result, [path for path in paths if path not in product_paths + horizontal_paths + source_paths + citation_detail_paths])]
    if drawer:
        groups.append((products, product_paths))
    for metadata, outputs in groups:
        files, seams = metadata.get("frame_files"), metadata.get("seams")
        if (not isinstance(files, list) or not files or any(not isinstance(path, str) for path in files)
                or len(set(files)) != len(files) or type(metadata.get("frame_count")) is not int
                or len(files) != metadata["frame_count"] or not isinstance(seams, list) or len(seams) != len(files) - 1):
            raise CaptureError("原始帧文件或接缝记录不完整")
        frames = []
        for file in files:
            path = Path(file).resolve()
            if not path.is_relative_to(debug) or path.suffix != ".png":
                raise CaptureError("原始帧不属于本题调试目录")
            frames.append(path.read_bytes())
        overlaps = []
        for index, seam in enumerate(seams):
            if not isinstance(seam, dict) or seam.get("verified") is not True or type(seam.get("overlap")) is not int:
                raise CaptureError("原始接缝记录无效")
            matcher = match_product_overlap if metadata is products else match_overlap
            measured = matcher(frames[index], frames[index + 1])
            if measured["overlap"] != seam["overlap"] or image_size(frames[index + 1])[1] <= measured["overlap"]:
                raise CaptureError("记录的接缝与原始帧不一致")
            overlaps.append(measured["overlap"])
        expected = compose_images(frames, overlaps)
        if len(expected) != len(outputs):
            raise CaptureError("交付图片数量与原始帧拼接结果不一致")
        for file, rebuilt in zip(outputs, expected):
            path = Path(file).resolve()
            if not path.is_relative_to(delivery) or path.suffix != ".png":
                raise CaptureError("正式图片不属于本题交付目录")
            with Image.open(path) as actual:
                if (actual.format != "PNG" or actual.size != rebuilt.size
                        or actual.convert("RGBA").tobytes() != rebuilt.convert("RGBA").tobytes()):
                    raise CaptureError("正式图片像素与原始帧拼接结果不一致")
