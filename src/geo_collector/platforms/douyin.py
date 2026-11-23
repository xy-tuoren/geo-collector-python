"""Douyin search and Xiaohe mini-app entry rules."""
from __future__ import annotations

import re

from ..device import map_bounds
from ..goals import role_nodes
from ..images import CaptureError, crop, match_overlap, stable
from .common import edit, enter_search_answer, exact, exact_question_visible, label_taps, labels, nodes, reenter_search_answer, tap


ID = "douyin"
PACKAGE = "com.ss.android.ugc.aweme"
REENTER_SUPPORTED = True
ENTRY_STEP = "douyin.open_xiaohe"
INPUT_HINTS = ("输入问题|搜索",)
SEND_HINTS = ("搜索",)
ANSWER_PAGE_ERROR = "抖音当前不是小程序页面，不能把视频或搜索页当作回答滚动"
ENTRY_SCAN_MAX_VIEWPORTS = 1
RESULT_READY_TIMEOUT = 12.0
ENTRY_LABELS = {"查看全文", "免费咨询", "立即咨询", "进入小程序"}
ENTRY_HINTS = ["小荷AI医生", "查看全文|免费咨询|立即咨询|进入小程序"]
ENTRY_TAGLINE = "字节跳动旗下医疗大模型应用"


def search_results_ready(observation, question):
    editor = edit(observation, ID)
    tabs = labels(observation) & {"综合", "视频", "用户", "直播", "图文", "商品"}
    return bool(observation.package == PACKAGE and editor
                and exact(editor.get("text", "")) == exact(question) and len(tabs) >= 2
                and "MiniAppHostActivity" not in observation.activity)


def results_loaded(observation, question):
    """Search identity alone does not prove that first-screen results loaded."""
    if not search_results_ready(observation, question):
        return False
    if any(node.get("class_name", "").endswith("ProgressBar") for node in nodes(observation)):
        return False
    height = observation.logical_size[1]
    content = [label for label, box in _label_boxes(observation)
               if box[1] >= height * .15 and box[3] <= height * .96 and label]
    if any(re.search(r"加载中|正在加载|正在搜索|搜索中|思考中|(?:^|[\s：:])(?:生成中|正在生成(?:回复|回答)?)(?:[.。…·\s]|$)|加载失败|网络异常|网络错误|网络不稳定|无法连接|网络连接失败", label)
           or re.fullmatch(r"(?:点击|请|重新)?重试|刷新重试", label) for label in content):
        return False
    return any(len(label) > 1 and label not in {"搜索", "搜索历史", "历史搜索", "猜你想搜", "相关搜索"} for label in content)


def miniapp_ready(observation):
    branded = any("小荷AI医生" in value for value in labels(observation))
    return observation.package == PACKAGE and branded and (
        "MiniAppHostActivity" in observation.activity or bool(role_nodes(observation, "answer_page")))


def _label_boxes(observation):
    values = [(exact(node.get("text") or node.get("description") or ""), node["bounds"]) for node in nodes(observation)]
    values.extend((exact(line["text"]), map_bounds(line["bounds"], observation.physical_size, observation.logical_size))
                  for line in observation.texts if line.get("confidence", 0) >= .78)
    return values


def _entry_label(label):
    return exact(label).rstrip(">＞") in ENTRY_LABELS


def _summary_header(brand, tagline, size):
    width, height = size
    return (height * .12 <= brand[1] < brand[3] <= height * .4
            and 0 <= tagline[1] - brand[3] <= height * .06
            and abs(tagline[0] - brand[0]) <= width * .08)


def _summary_card(brand, action, containers, size):
    width, height = size
    return any(parent[0] <= brand[0] < brand[2] <= parent[2]
               and parent[0] <= action[0] < action[2] <= parent[2]
               and parent[1] <= brand[1] < brand[3] <= action[1] < action[3] <= parent[3]
               and parent[2] - parent[0] >= width * .65
               and parent[3] - parent[1] <= height * .55
               and (parent[2] - parent[0]) * (parent[3] - parent[1]) <= width * height * .50
               for parent in containers)


def _same_entry_card(brand, action, taglines, containers, size, label):
    width, height = size
    close = (max(brand[1], action[1]) - min(brand[3], action[3]) <= height * .08
             and max(brand[3], action[3]) - min(brand[1], action[1]) <= height * .22
             and max(brand[0], action[0]) - min(brand[2], action[2]) <= width * .20
             and max(brand[2], action[2]) - min(brand[0], action[0]) <= width * .95)
    summary = exact(label).rstrip(">＞") == "查看全文" and any(_summary_header(brand, tagline, size)
                  and 0 <= action[1] - tagline[3] <= height * .3
                  and action[3] <= height * .65
                  and abs((action[0] + action[2]) / 2 - width / 2) <= width * .28
                  and _summary_card(brand, action, containers, size)
                  for tagline in taglines)
    return close or summary


def platform_entry_visible(observation):
    """Conservative evidence that a Xiaohe result card may need a learned locator."""
    visible = _label_boxes(observation)
    brands = [box for label, box in visible if label == "小荷AI医生"]
    actions = [(label, box) for label, box in visible if _entry_label(label)]
    taglines = [box for label, box in visible if label == ENTRY_TAGLINE]
    size = observation.logical_size
    containers = [node["bounds"] for node in nodes(observation)]
    return (any(_same_entry_card(brand, action, taglines, containers, size, label)
                for brand in brands for label, action in actions)
            or any(_summary_header(brand, tagline, size) for brand in brands for tagline in taglines))


def result_scroll_shift(first, second):
    if (first.logical_size, first.physical_size) != (second.logical_size, second.physical_size):
        raise ValueError("入口扫描期间窗口尺寸发生变化")
    width, height = first.physical_size
    bounds = (0, height * .15, width, height * .96)
    if stable(first.png, second.png, bounds=bounds):
        return 0
    try:
        seam = match_overlap(crop(first.png, bounds), crop(second.png, bounds), expected_shift=height * .45)
    except CaptureError as error:
        raise ValueError("滑动后无法用原始像素证明结果视口连续") from error
    if seam.get("verified") is not True:
        raise ValueError("滑动后的结果视口接缝未经验证")
    return 0 if seam["shift"] <= height * .02 else seam["shift"]


def same_result_viewport(first, second):
    try:
        return result_scroll_shift(first, second) == 0
    except ValueError:
        return False


def scan_progress(observations, *, maximum=ENTRY_SCAN_MAX_VIEWPORTS):
    """Validate evidence; multi-screen handling is only for historical results."""
    if not observations:
        return {"viewports_checked": 0, "limit_reached": False, "end_confirmed": False}
    geometry = observations[0].logical_size, observations[0].physical_size
    viewports, stationary = 1, 0
    for index, current in enumerate(observations[1:], 1):
        if (current.logical_size, current.physical_size) != geometry:
            raise ValueError("入口扫描期间窗口尺寸发生变化")
        shift = result_scroll_shift(observations[index - 1], current)
        if shift == 0:
            stationary += 1
        else:
            if any(same_result_viewport(previous, current) for previous in observations[:index - 1]):
                raise ValueError("入口扫描画面回到了更早视口，无法证明连续范围")
            viewports += 1
            stationary = 0
        if index < len(observations) - 1 and (viewports >= maximum or stationary >= 2):
            raise ValueError("入口扫描在已达到终点后仍包含额外观察")
    return {"viewports_checked": viewports, "limit_reached": viewports >= maximum,
            "end_confirmed": stationary >= 2}


def brand_entry_defaults(observation):
    """Require brand and CTA in a small shared card; do not follow generic AI."""
    lw, lh = observation.logical_size
    visible_nodes = nodes(observation)
    brands = [node["bounds"] for node in visible_nodes
              if exact(node.get("text") or node.get("description") or "") == "小荷AI医生"]
    brands.extend(map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
                  for line in observation.texts if line.get("confidence", 0) >= .78 and exact(line["text"]) == "小荷AI医生")
    defaults = []
    for node in visible_nodes:
        label = str(node.get("text") or node.get("description") or "").strip()
        if not _entry_label(label):
            continue
        button = node["bounds"]
        for brand in brands:
            shared = [parent["bounds"] for parent in visible_nodes
                      if parent["bounds"][0] <= min(brand[0], button[0]) and parent["bounds"][1] <= min(brand[1], button[1])
                      and parent["bounds"][2] >= max(brand[2], button[2]) and parent["bounds"][3] >= max(brand[3], button[3])
                      and .2 * lw <= parent["bounds"][2] - parent["bounds"][0]
                      and (parent["bounds"][2] - parent["bounds"][0]) * (parent["bounds"][3] - parent["bounds"][1]) <= lw * lh * .30]
            if not shared:
                continue
            card = min(shared, key=lambda bounds: (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
            region = [max(0, card[0] / lw), max(0, card[1] / lh), min(1, card[2] / lw), min(1, card[3] / lh)]
            attr = "resource_id" if node.get("resource_id") else "text" if node.get("text") else "description"
            defaults.append(tap(attr, node[attr], region))
    if defaults:
        return defaults
    visible = _label_boxes(observation)
    taglines = [box for label, box in visible if label == ENTRY_TAGLINE]
    for line in observation.texts:
        if line.get("confidence", 0) < .78 or not _entry_label(line["text"]):
            continue
        button = map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
        containers = [node["bounds"] for node in visible_nodes]
        if not any(_same_entry_card(brand, button, taglines, containers, observation.logical_size, line["text"])
                   for brand in brands):
            continue
        l, t, r, b = button
        region = [max(0, l / lw - .2), max(0, t / lh - .06), min(1, r / lw + .2), min(1, b / lh + .06)]
        label = exact(line["text"]).rstrip(">＞")
        defaults.append({"actions": [{"type": "tap", "target": {
            "kind": "ocr", "pattern": "^" + re.escape(label) + r"[>＞]?$", "region": region}}]})
    return defaults


def open(engine, task):
    defaults = label_taps(["搜索"])
    for close in [*label_taps(["关闭", "close"], [.75, 0, 1, .16]), {"actions": [{"type": "back"}]}]:
        defaults.extend({"actions": [*close["actions"], *search["actions"]]} for search in label_taps(["搜索"]))
    engine.perform("douyin.open_search", "打开抖音搜索页并确认搜索输入框", PACKAGE,
                   lambda current: edit(current, ID) is not None,
                   defaults=defaults, hints=["搜索"])


def focus_defaults():
    return [tap("resource_id", PACKAGE + ":id/et_search_kw")]


def send_defaults():
    return label_taps(["搜索"], [.7, 0, 1, .2])


def sent(observation, task):
    return observation.package == PACKAGE and (
        search_results_ready(observation, task["text"])
        or (miniapp_ready(observation) and exact_question_visible(observation, task["text"])))


def can_enter_current(observation, task):
    return search_results_ready(observation, task["text"])


def enter_answer(engine, task, evidence_dir=None):
    return enter_search_answer(engine, task, evidence_dir)


def answer_ready(observation):
    return miniapp_ready(observation)


def reenter_answer(engine, task, observation):
    return reenter_search_answer(engine, task, observation)
