"""Native Xiaohe navigation and submission rules."""
from __future__ import annotations

import numpy as np

from ..capture import drawer_state
from ..device import map_bounds
from ..images import CaptureError, _image
from .common import edit, exact, exact_question_visible, label_taps, nodes, tap


ID = "xiaohe"
PACKAGE = "com.aurora.xiaohe.aidoctor"
REENTER_SUPPORTED = False
ENTRY_STEP = None
INPUT_HINTS = ("输入问题|搜索",)
SEND_HINTS = ("发送",)
ANSWER_PAGE_ERROR = "小荷当前不是可验证的回答页面，不能继续滚动"


def clean_session(observation):
    """Prove an unused composer and body, including the greeting-free home."""
    if observation.package != PACKAGE:
        return False
    editor = edit(observation, ID)
    if editor is None or str(editor.get("text") or "").strip():
        return False
    lw, lh = observation.logical_size
    visible_nodes = nodes(observation)
    visible_labels = [(exact(label), node["bounds"]) for node in visible_nodes
                      for label in (node.get("text"), node.get("description")) if label]
    visible_labels.extend((exact(line["text"]), map_bounds(line["bounds"], observation.physical_size, observation.logical_size))
                          for line in observation.texts if line.get("confidence", 0) >= .78)
    headers = [box for label, box in visible_labels if label == "小荷AI医生" and 0 <= box[1] < box[3] <= lh * .18]
    if not headers or not lh * .6 <= editor["bounds"][1] < lh:
        return False
    top = max(box[3] for box in headers)
    navigation = {"小荷AI医生", "开启新会话", "新会话", "更多", "历史记录", "语音播报", "语音通话"}
    for node in visible_nodes:
        label = exact(node.get("text") or node.get("description") or "")
        box = node["bounds"]
        picker = node.get("description") in {"不选择咨询人，随便聊聊", "本人，新增咨询人档案", "新建咨询人"}
        picker = picker or (label == "不选咨询人" and node.get("class_name", "").endswith("Button"))
        if ((label in navigation and box[3] <= lh * .18) or (picker and box[3] <= lh * .25)) and 0 < box[3] - box[1] < lh * .1:
            top = max(top, box[3])
    bottom = editor["bounds"][1]
    for node in visible_nodes:
        label = exact(node.get("text") or node.get("description") or "")
        box = node["bounds"]
        if label in {"报告解读", "拍患处", "拍药品"} and (node.get("description") or node.get("class_name", "").endswith("Button")):
            if lh * .65 <= box[1] < box[3] <= editor["bounds"][1] and box[3] - box[1] < lh * .15:
                bottom = min(bottom, box[1])
    if bottom - top < lh * .35:
        return False
    greetings = []
    for label, box in visible_labels:
        if box[3] <= top or box[1] >= bottom:
            continue
        if "有任何健康问题" in label and "随时问我" in label and box[3] - box[1] < lh * .15:
            greetings.append(box)
        else:
            return False
    try:
        image = _image(observation.png)
        if image.size != tuple(observation.physical_size):
            return False
        pw, ph = image.size
        _, y1, _, y2 = map_bounds((0, top, lw, bottom), observation.logical_size, observation.physical_size)
        padding = max(2, min(3, round(ph * .001)))
        pixels = np.asarray(image, dtype=np.int16)[round(y1) + padding:round(y2) - padding].copy()
        if pixels.size == 0:
            return False
        for box in greetings:
            left, upper, right, lower = map_bounds(box, observation.logical_size, observation.physical_size)
            pixels[max(0, round(upper) - round(y1) - padding * 2):min(len(pixels), round(lower) - round(y1) + padding),
                   max(0, round(left) - padding):min(pw, round(right) + padding + 1)] = 255
        background = np.median(pixels.reshape(-1, 3), axis=0)
        return bool(background.min() >= 240 and background.max() - background.min() <= 8
                    and np.max(np.abs(pixels - background)) <= 10)
    except (CaptureError, ValueError):
        return False


def open(engine, task):
    if drawer_state(engine.observe()) is not None:
        engine.perform("products.close", "关闭上题已完成的药品列表，准备下一题新会话", PACKAGE,
                       lambda current: drawer_state(current) is None,
                       defaults=[{"actions": [{"type": "back"}]}])
    defaults = label_taps(["开启新会话", "新会话"])
    for more in label_taps(["更多"]):
        for new in label_taps(["开启新会话", "新会话"]):
            defaults.append({"actions": [*more["actions"], *new["actions"]]})
    engine.perform("xiaohe.new_session", "进入没有旧回答的新会话首页", PACKAGE, clean_session,
                   defaults=defaults, hints=["开启新会话|新会话", "更多"])


def focus_defaults():
    return [tap("class_name", "android.widget.EditText"), *label_taps(["输入问题", "输入问题 或 按住说话"])]


def send_defaults():
    return label_taps(["发送"])


def sent(observation, task):
    return observation.package == PACKAGE and exact_question_visible(observation, task["text"])


def can_enter_current(observation, task):
    return False


def enter_answer(engine, task, evidence_dir=None):
    return None


def answer_ready(observation):
    return observation.package == PACKAGE
