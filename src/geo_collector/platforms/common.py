"""Small selector helpers shared by explicit platform flows."""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

from ..device import DeviceError
from ..goals import role_nodes
from ..recovery import StepSatisfied, load_observation, save_observation
from ..storage import read_json, write_json


def exact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))


def nodes(observation):
    return [node for node in observation.nodes
            if node.get("package") in (None, "", observation.package) and node.get("enabled", True)]


def labels(observation):
    values = [str(node.get("text") or node.get("description") or "") for node in nodes(observation)]
    values.extend(str(line["text"]) for line in observation.texts if line.get("confidence", 0) >= .78)
    return {exact(value) for value in values if value}


def edits(observation, entry):
    learned = role_nodes(observation, "input")
    if learned:
        fields = [node for node in learned if node.get("class_name", "").endswith("EditText")]
        if entry in {"douyin", "toutiao"}:
            width, height = observation.logical_size
            fields = [node for node in fields if 0 <= node["bounds"][1] < node["bounds"][3] <= height * .16
                      and node["bounds"][2] - node["bounds"][0] >= width * .35]
        return fields
    if entry == "toutiao":
        width, height = observation.logical_size
        fields = [node for node in nodes(observation)
                  if 0 <= node["bounds"][1] < node["bounds"][3] <= height * .16
                  and node["bounds"][2] - node["bounds"][0] >= width * .35
                  and (node.get("resource_id", "").endswith(":id/cx")
                       or node.get("class_name", "").endswith("EditText")
                       or (node.get("clickable") and re.match(r"^搜索框[，,]", node.get("text", ""))))]
        return [{**node, "text": re.sub(r"^搜索框[，,]\s*", "", node.get("text", ""))}
                if not node.get("class_name", "").endswith("EditText") else node for node in fields]
    return [node for node in nodes(observation) if
            (node.get("resource_id", "").endswith(":id/et_search_kw") if entry == "douyin"
             else node.get("class_name", "").endswith("EditText"))]


def edit(observation, entry, *, focused=False):
    fields = edits(observation, entry)
    if focused:
        fields = [node for node in fields if node.get("focused") and (
            node.get("class_name", "").endswith("EditText")
            or entry == "toutiao" and node.get("resource_id", "").endswith(":id/d0") and node.get("clickable"))]
        all_focused = [node for node in nodes(observation)
                       if node.get("focused") and (
                           node.get("class_name", "").endswith("EditText")
                           or entry == "toutiao" and node.get("resource_id", "").endswith(":id/d0") and node.get("clickable"))]
        if len(all_focused) != 1:
            return None
    return fields[0] if len(fields) == 1 else None


def tap(attr, value, region=None):
    target = {"kind": "node", "attr": attr, "value": value}
    if region:
        target["region"] = region
    return {"actions": [{"type": "tap", "target": target}]}


def label_taps(values, region=None):
    return [tap(attr, label, region) for label in values for attr in ("description", "text")]


def exact_question_visible(observation, question):
    """A matching editor is never proof that a question was submitted."""
    from ..capture import answer_viewport, question_bubble
    from ..images import CaptureError
    try:
        return question_bubble(observation, question, answer_viewport(observation)) is not None
    except CaptureError:
        return False


def _open_search_entry(engine, task, observation, platform):
    def before_entry():
        current = engine.observe()
        if (not platform.search_results_ready(current, task["text"])
                or not (platform.brand_entry_defaults(current) or platform.platform_entry_visible(current))):
            raise DeviceError("入口点击前无法确认当前原题及小荷品牌卡片，未点击。")
        save_observation(engine.run_dir / "调试产物" / task["entry"] / f"{task['index']:03d}" / "entry-source", current)

    return engine.perform(platform.ENTRY_STEP, "打开当前搜索结果中属于小荷AI医生的回答或咨询入口",
                          platform.PACKAGE, platform.answer_ready, defaults=platform.brand_entry_defaults(observation),
                          hints=platform.ENTRY_HINTS, before_action=before_entry)


def enter_search_answer(engine, task, evidence_dir=None):
    """Shared first-screen evidence loop; each platform owns its page checks."""
    from . import PLATFORMS
    platform = PLATFORMS[task["entry"]]
    entry, package = platform.ID, platform.PACKAGE
    ready_step = entry + ".results_ready"
    evidence_dir = Path(evidence_dir or engine.run_dir / "调试产物" / entry / f"{task['index']:03d}") / "entry-scan"
    identity = {"question": task["text"], "package": package, "entry": entry, "index": task["index"]}
    checkpoint = engine.state.get("entry_scan_resume")
    if checkpoint:
        if (any(checkpoint.get(key) != value for key, value in identity.items())
                or checkpoint.get("max_viewports") not in ({1, 5} if entry == "douyin" else {1})):
            raise DeviceError("入口扫描断点不属于当前题目、平台或扫描范围，已停止。")
    else:
        checkpoint = {**identity, "max_viewports": 1, "observation_paths": []}
        engine.state["entry_scan_resume"] = checkpoint
        engine.save()
    observations = checkpoint["observation_paths"]
    if not isinstance(observations, list) or any(not isinstance(path, str) for path in observations):
        raise DeviceError("入口扫描断点中的观察路径无效。")
    if len(observations) > checkpoint["max_viewports"] * 2 - 1:
        raise DeviceError("入口扫描断点超过规定范围。")
    if observations:
        # Historical Douyin checkpoints reuse only their original first screen.
        directory = Path(observations[0]).resolve()
        try:
            if directory != (evidence_dir / "001").resolve() or not (directory / "screen.xml").is_file():
                raise ValueError("首屏观察路径或层级文件无效")
            observation = load_observation(directory)
            if (not platform.results_loaded(observation, task["text"])
                    or platform.brand_entry_defaults(observation) or platform.platform_entry_visible(observation)):
                raise ValueError("保存的首屏不能证明结果已加载且无小荷入口")
        except (OSError, ValueError) as error:
            raise DeviceError(f"首屏断点无效：{error}") from error
        observations[:] = observations[:1]
    else:
        observation = engine.observe()
        if platform.answer_ready(observation):
            engine.state.pop("entry_scan_resume", None)
            engine.save()
            return None
        if engine.state.get("exploration", {}).get("step") == platform.ENTRY_STEP:
            engine.state.pop("entry_scan_resume", None)
            engine.save()
            _open_search_entry(engine, task, observation, platform)
            return None
        deadline = time.monotonic() + platform.RESULT_READY_TIMEOUT
        while True:
            if not platform.search_results_ready(observation, task["text"]):
                raise DeviceError("当前搜索结果不属于完整原题，未打开小程序。")
            if platform.results_loaded(observation, task["text"]):
                break
            if time.monotonic() >= deadline:
                try:
                    engine._request(ready_step, "等待当前原题首屏搜索结果加载完成；不滚动、不重新搜索，不把加载或网络异常当作无入口", observation, (),
                                    "首屏结果仍为空、加载中或网络异常", postcondition=lambda item: platform.results_loaded(item, task["text"]))
                except StepSatisfied as done:
                    observation = done.observation
                    continue
            time.sleep(.5)
            observation = engine.observe()
        engine.resolve_capture_request(ready_step)
        if platform.brand_entry_defaults(observation) or platform.platform_entry_visible(observation):
            engine.state.pop("entry_scan_resume", None)
            engine.save()
            engine.log(entry + "_entry_found", viewport=1)
            _open_search_entry(engine, task, observation, platform)
            return None
        directory = evidence_dir / "001"
        save_observation(directory, observation)
        observations.append(str(directory.resolve()))

    checkpoint.update(max_viewports=1, scan_complete=True)
    checkpoint.pop("swipe_inflight", None)
    engine.save()
    result = {"outcome": "entry_not_found", "search_query_confirmed": True, "search_results_confirmed": True,
              "platform_entry_found": False, "scan_complete": True,
              "scan_scope": {"type": "first_screen", "max_viewports": 1, **platform.scan_progress([observation])},
              "observation_paths": list(observations)}
    pending = engine.state.get("pending_request")
    if pending:
        file = Path(pending)
        request = read_json(file)
        if (request.get("step"), request.get("entry"), request.get("question_index")) != (platform.ENTRY_STEP, entry, task["index"]):
            raise DeviceError("无入口结果与当前修复断点不属于同一步骤，已停止。")
        request.update(status="resolved_by_entry_not_found", finished_at=time.time())
        write_json(file, request)
        engine.state.pop("pending_request", None)
    engine.save()
    return result


def reenter_search_answer(engine, task, observation):
    from . import PLATFORMS
    platform = PLATFORMS[task["entry"]]
    if platform.answer_ready(observation):
        engine.perform(platform.ID + ".close_miniapp", "关闭当前加载失败的小荷页面，回到原题搜索结果", platform.PACKAGE,
                       lambda current: platform.search_results_ready(current, task["text"]),
                       defaults=[{"actions": [{"type": "back"}]}])
    if not platform.search_results_ready(engine.observe(), task["text"]):
        raise DeviceError("原题搜索结果未保留；停止恢复，没有重新搜索或发送")
