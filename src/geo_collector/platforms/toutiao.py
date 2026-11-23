"""Toutiao search: branded summary or mini-app card, followed by shared capture."""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from ..capture import answer_page_ready, answer_viewport, drawer_state
from ..device import DeviceError, LaunchForegroundError, map_bounds
from ..goals import role_nodes
from ..images import CaptureError
from ..recovery import save_observation
from .common import edit, enter_search_answer, exact, exact_question_visible, label_taps, nodes, reenter_search_answer, tap


ID = "toutiao"
PACKAGE = "com.ss.android.article.news"
REENTER_SUPPORTED = True
ENTRY_STEP = "toutiao.open_xiaohe"
INPUT_HINTS = ("搜索框|搜索",)
SEND_HINTS = ("搜索",)
ENTRY_HINTS = ("小荷AI医生", "查看更多|查看全文", "小程序")
ANSWER_PAGE_ERROR = "头条当前不是可验证的小荷全文页，不能把搜索或普通网页当作回答滚动"
ENTRY_SCAN_MAX_VIEWPORTS = 1
RESULT_READY_TIMEOUT = 30.0
SUMMARY = r"^小荷AI医生(?:[·・]?AI)?[·・]?智能总结$"
TABS = {"综合", "资讯", "视频", "图片", "用户", "问答", "微头条", "商品"}


def _labels(observation):
    width, height = observation.logical_size
    records = []
    for node in nodes(observation):
        label = node.get("text") or node.get("description") or ""
        box = node["bounds"]
        if not label or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height:
            continue
        attr = "resource_id" if node.get("resource_id") else "text" if node.get("text") else "description"
        records.append({"text": exact(label), "bounds": box, "clickable": node.get("clickable", False),
                        "target": {"kind": "node", "attr": attr, "value": node[attr]}})
    for line in observation.texts:
        if line.get("confidence", 0) < .85:
            continue
        label = exact(line["text"])
        box = map_bounds(line["bounds"], observation.physical_size, observation.logical_size)
        if any(row["text"] == label and _overlap(row["bounds"], box) for row in records):
            continue
        records.append({"text": label, "bounds": box, "target": {"kind": "ocr", "pattern": "^" + re.escape(line["text"]) + "$"}})
    return records


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def search_results_ready(observation, question):
    editor = edit(observation, ID)
    if observation.package != PACKAGE or not editor or editor.get("text", "") != question or "MiniAppHostActivity" in observation.activity:
        return False
    if editor.get("focused"):
        return False
    height = observation.logical_size[1]
    tabs = {row["text"] for row in _labels(observation) if row["text"] in TABS
            and editor["bounds"][3] <= row["bounds"][1] < row["bounds"][3] <= height * .25}
    return len(tabs) >= 2


def results_loaded(observation, question):
    if not search_results_ready(observation, question):
        return False
    if any(node.get("class_name", "").endswith("ProgressBar") for node in nodes(observation)):
        return False
    height = observation.logical_size[1]
    content = [row["text"] for row in _labels(observation) if height * .15 <= row["bounds"][1] < row["bounds"][3] <= height * .96]
    if any(re.search(r"加载中|正在加载|正在搜索|搜索中|加载失败|网络异常|网络错误|网络不稳定|无法连接|网络连接失败", text)
           or re.fullmatch(r"(?:点击|请|重新)?重试|刷新重试", text) for text in content):
        return False
    chrome = TABS | {"搜索", "搜索历史", "历史搜索", "猜你想搜", "相关搜索", "筛选", "全部", exact(question)}
    return any(len(text) >= 4 and text not in chrome and not text.startswith("搜索框") for text in content)


def _entry_targets(observation):
    if observation.package != PACKAGE:
        return []
    width, height = observation.logical_size
    visible = _labels(observation)
    targets = []
    for summary in (row for row in visible if re.fullmatch(SUMMARY, row["text"])):
        box = summary["bounds"]
        for button in (row for row in visible if row["text"] in {"查看更多", "查看全文"}):
            other = button["bounds"]
            maximum_width = .95 if button["target"]["kind"] == "node" and button.get("clickable") else .35
            if (height * .1 <= (box[1] + box[3]) / 2 <= height * .68
                    and 0 <= other[1] - box[3] <= height * .45
                    and min(box[2], other[2]) > max(box[0], other[0])
                    and width * .08 <= other[2] - other[0] <= width * maximum_width
                    and other[3] - other[1] <= height * .12
                    and abs((other[0] + other[2]) / 2 - width / 2) <= width * .28):
                targets.append(button)
    for brand in (row for row in visible if re.fullmatch(r"小荷AI医生(?:小程序)?", row["text"])):
        box = brand["bounds"]
        if not height * .12 <= box[1] < box[3] <= height * .94:
            continue
        badges = [row["bounds"] for row in visible if row["text"] == "小程序"]
        if brand["text"].endswith("小程序") or any(
                (abs((other[1] + other[3] - box[1] - box[3]) / 2) <= max(other[3] - other[1], box[3] - box[1])
                 and 0 <= other[0] - box[2] <= width * .12)
                or (0 <= other[1] - box[3] <= height * .06 and abs(other[0] - box[0]) <= width * .08)
                for other in badges):
            targets.append(brand)
    unique = []
    for target in targets:
        if not any(_overlap(target["bounds"], existing["bounds"]) for existing in unique):
            unique.append(target)
    return unique


def brand_entry_defaults(observation):
    targets = _entry_targets(observation)
    if len(targets) != 1:
        return []
    row = targets[0]
    width, height = observation.logical_size
    region = [value / (width if index % 2 == 0 else height) for index, value in enumerate(row["bounds"])]
    return [{"actions": [{"type": "tap", "target": {**row["target"], "region": region}}]}]


def platform_entry_visible(observation):
    # A summary whose button is not yet locatable needs repair, not an absence result.
    return observation.package == PACKAGE and (bool(_entry_targets(observation))
            or any(re.fullmatch(SUMMARY, row["text"]) for row in _labels(observation)))


def answer_ready(observation):
    if observation.package != PACKAGE or edit(observation, ID) is not None:
        return False
    visible = nodes(observation)
    branded = any(row["text"] == "小荷AI医生" for row in _labels(observation))
    if not branded:
        return False
    width, height = observation.logical_size
    legacy = (any(node.get("class_name") == "android.webkit.WebView" and node.get("text") == "小荷AI医生" for node in visible)
              and any(node.get("resource_id") == PACKAGE + ":id/title" and node.get("text") == "小荷AI医生"
                      and node["bounds"][3] <= height * .12 for node in visible)
              and any(re.fullmatch(r"返回(?:[，,]按钮)?", node.get("description", "")) and node.get("clickable")
                      and node["bounds"][2] <= width * .2 and node["bounds"][3] <= height * .12 for node in visible)
              and any(row["text"].startswith("AI生成非医疗诊断") and row["bounds"][3] <= height * .2 for row in _labels(observation)))
    if not ("MiniAppHostActivity" in observation.activity or role_nodes(observation, "answer_page") or legacy):
        return False
    if drawer_state(observation) is not None:
        return True
    if not answer_page_ready(observation):
        return False
    try:
        answer_viewport(observation)
    except CaptureError:
        return False
    return True


def _confirm_entry_restart(engine, task, current=None):
    restart = task["entry_restart"]
    directory = Path(restart["before"]).parent
    # A delayed Android task transition permits observations, never another launch.
    for attempt in range(6):
        current = current if current is not None else engine.observe()
        path = directory / f"after-{len(restart.get('observations', [])) + 1:03d}"
        save_observation(path, current)
        verified = current.package == PACKAGE and "BrowserActivity" not in current.activity
        restart.setdefault("observations", []).append(str(path))
        restart.update(status="verified" if verified else "waiting", after=str(path))
        engine.save()
        engine.log("entry_restart_observed", verified=verified, package=current.package, activity=current.activity,
                   evidence=str(path))
        if verified:
            engine.log("entry_restart_finished", verified=True, evidence=str(path))
            return
        if attempt < 5:
            time.sleep(1)
            current = None
    restart["status"] = "failed"
    engine.save()
    engine.log("entry_restart_finished", verified=False, evidence=restart["after"])
    raise DeviceError("头条重启后未回到目标应用的安全入口，未执行搜索或输入")


def _normalize_entry(engine, task):
    """Discard a restored browser task only before any query is submitted.

    Android may restore a BrowserActivity whose Back exits to another app.
    Relaunching that old page cannot establish a safe search navigation path.
    """
    if task.get("phase") != "open" or engine.state.get("mode") != "batch":
        return
    current = engine.observe()
    restart = task.get("entry_restart")
    if current.package == PACKAGE and "BrowserActivity" not in current.activity and (not restart or restart.get("status") == "verified"):
        return
    if (engine.state.get("phase"), engine.state.get("entry"), engine.state.get("index")) != ("open", ID, task["index"]):
        raise DeviceError("头条入口恢复与当前题目阶段不符，未执行重启")
    if any(engine.state.get(key) for key in ("pending_request", "inflight", "exploration")):
        raise DeviceError("头条入口仍有待处理或不确定动作，未执行重启")
    if restart:
        if restart.get("status") == "verified":
            raise DeviceError("头条入口已尝试恢复，仍不是安全搜索入口，需要检查现场")
        return _confirm_entry_restart(engine, task, current)
    directory = engine.run_dir / "调试产物" / ID / f"{task['index']:03d}" / ("entry-restart-" + uuid.uuid4().hex)
    save_observation(directory / "before", current)
    task["entry_restart"] = {"status": "started", "before": str(directory / "before"),
                             "reason": "restored_browser" if current.package == PACKAGE else "foreign_foreground"}
    engine.save()
    engine.log("entry_restart_started", package=current.package, activity=current.activity, evidence=str(directory))
    try:
        engine.device.launch(PACKAGE, restart=True)
    except LaunchForegroundError as error:
        task["entry_restart"]["launch_error"] = str(error)
        engine.save()
        engine.log("entry_restart_launch_delayed", error=str(error), evidence=str(directory))
    _confirm_entry_restart(engine, task)


def open(engine, task):
    _normalize_entry(engine, task)
    search = [tap("resource_id", PACKAGE + ":id/" + suffix, [0, 0, 1, .16])
              for suffix in ("kic", "search_bar_search_icon")]
    search += label_taps(["搜索", "搜你想看的"], [0, 0, 1, .16])
    defaults = list(search)
    for close in [*label_taps(["关闭", "close"], [.75, 0, 1, .16]),
                  *label_taps(["返回，按钮", "返回,按钮", "返回"], [0, 0, .2, .16]), {"actions": [{"type": "back"}]}]:
        defaults.append(close)
        defaults.extend({"actions": [*close["actions"], *target["actions"]]} for target in search)
    engine.perform("toutiao.open_search", "打开头条搜索页并确认唯一可回读搜索框", PACKAGE,
                   lambda current: edit(current, ID) is not None, defaults=defaults, hints=INPUT_HINTS)


def focus_defaults():
    return [tap("resource_id", PACKAGE + ":id/" + suffix, [0, 0, 1, .16]) for suffix in ("cx", "d0")] + [
        tap("class_name", "android.widget.EditText", [0, 0, 1, .16])]


def send_defaults():
    return [tap("resource_id", PACKAGE + ":id/e0", [.7, 0, 1, .2]), *label_taps(["搜索"], [.7, 0, 1, .2])]


def sent(observation, task):
    return search_results_ready(observation, task["text"]) or (answer_ready(observation) and exact_question_visible(observation, task["text"]))


def can_enter_current(observation, task):
    return search_results_ready(observation, task["text"])


def enter_answer(engine, task, evidence_dir=None):
    return enter_search_answer(engine, task, evidence_dir)


def reenter_answer(engine, task, observation):
    return reenter_search_answer(engine, task, observation)


def scan_progress(observations, *, maximum=ENTRY_SCAN_MAX_VIEWPORTS):
    if maximum != 1 or len(observations) > 1:
        raise ValueError("头条只支持原题首屏入口检查")
    return {"viewports_checked": len(observations), "limit_reached": bool(observations), "end_confirmed": False}
