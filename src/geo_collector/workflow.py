"""Question submission and resumable entry navigation, separate from capture."""
from __future__ import annotations

from pathlib import Path
import time

from .capture import AnswerCapture
from .device import DeviceError
from .images import CaptureError
from .goals import CONTRACT
from .platforms import PACKAGES, PLATFORMS
from .platforms.common import edit as _edit, exact_question_visible
from .recovery import NeedsRepair, StepEngine, save_observation
from .storage import read_json, write_json


PHASES = {"start", "open", "input", "submitting", "submitted", "capture", "done"}

# Compatibility aliases for callers and tests; implementations live with each platform.
clean_native_session = PLATFORMS["xiaohe"].clean_session
search_results_ready = PLATFORMS["douyin"].search_results_ready
_miniapp_ready = PLATFORMS["douyin"].miniapp_ready
_brand_entry_defaults = PLATFORMS["douyin"].brand_entry_defaults


def _phase(engine, task, phase):
    task["phase"] = phase
    engine.state.update(entry=task["entry"], index=task["index"], phase=phase)
    engine.save()


def _resolve_readonly_request(engine):
    pending = engine.state.get("pending_request")
    if pending:
        request_file = Path(pending)
        request = read_json(request_file)
        if (request.get("entry"), request.get("question_index"), request.get("step")) != (engine.state["entry"], engine.state["index"], engine.state["entry"] + ".send"):
            raise RuntimeError("发送恢复请求与当前题目身份不匹配。")
        request.update(status="satisfied_without_replay", finished_at=time.time())
        write_json(request_file, request)
        engine.state.pop("pending_request", None)
    engine.state.pop("inflight", None)
    engine.state["status"] = "running"
    engine.save()


def _sent(observation, task):
    return PLATFORMS[task["entry"]].sent(observation, task)


def _uncertain(engine, task, observation, reason):
    step = task["entry"] + ".send"
    engine._request(step, "只读确认当前完整问题已提交；不能重新输入、发送或新建会话", observation, (), reason, uncertain_send=True)
    raise RuntimeError("发送状态不确定，已拒绝重放。")


def _open(engine, task):
    PLATFORMS[task["entry"]].open(engine, task)


def _input_and_send(engine, task):
    entry, question = task["entry"], task["text"]
    platform, package = PLATFORMS[entry], PACKAGES[entry]
    if callable(submit := getattr(platform, "submit", None)):
        return submit(engine, task)
    observation = engine.perform(entry + ".focus_input", "聚焦唯一的问题输入框", package,
                                 lambda current: _edit(current, entry, focused=True) is not None,
                                 defaults=platform.focus_defaults(), hints=platform.INPUT_HINTS)
    editor = _edit(observation, entry, focused=True)
    if editor is None:
        raise DeviceError("未确认唯一聚焦输入框，已停止输入。")
    if str(editor.get("text", "")) != question:
        engine.device.input_text(question, expected_package=package, clear=True)
        observation = engine.observe()
        editor = _edit(observation, entry, focused=True)
        if editor is None or str(editor.get("text", "")) != question:
            raise DeviceError("输入后无法完整回读原题，未执行发送。")
    engine.log("question_input_verified", characters=len(question), entry_id=entry)
    def before_send():
        current = engine.observe()
        field = _edit(current, entry, focused=True)
        if current.package != package or field is None or str(field.get("text", "")) != question:
            raise DeviceError("发送前原题或唯一聚焦输入框已变化，未执行发送。")
        _phase(engine, task, "submitting")

    engine.perform(entry + ".send", "提交已经完整回读的问题，并确认该完整问题已出现", package,
                   lambda current: _sent(current, task), defaults=platform.send_defaults(), hints=platform.SEND_HINTS,
                   risk="send", force=True, before_action=before_send)
    _phase(engine, task, "submitted")


def _enter_answer(engine, task, debug=None):
    return PLATFORMS[task["entry"]].enter_answer(engine, task, debug)


def _finish_entry_outcome(engine, task, debug, result):
    result.update(entry=task["entry"], index=task["index"], question=task["text"],
                  status="complete", goal_contract_version=CONTRACT["version"])
    file = debug / "entry-not-found.json"
    write_json(file, result)
    task.update(status="complete", outcome=result["outcome"], result=str(file))
    engine.state.pop("entry_scan_resume", None)
    _phase(engine, task, "done")
    engine.log("question_complete", outcome=result["outcome"], result=str(file))


def _save_failure(engine, task, directory, error):
    details = {"status": "needs_repair" if isinstance(error, NeedsRepair) else "incomplete", "entry": task["entry"],
               "index": task["index"], "question": task["text"], "phase": task["phase"], "error": str(error), "type": type(error).__name__}
    try:
        save_observation(directory / "failure", engine.observe())
    except Exception as diagnostic_error:
        details["observation_error"] = str(diagnostic_error)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "failure.png").write_bytes(engine.device.screenshot())
        except Exception as screenshot_error:
            details["screenshot_error"] = str(screenshot_error)
    write_json(directory / "failure.json", details)


def recover_current(device, store, run_dir, state, action, reason):
    """Recorded recovery of the existing question; never reset to an input phase."""
    task = next((item for item in state["tasks"] if item.get("status") != "complete"), None)
    if not reason.strip() or task is None or task.get("phase") not in {"submitted", "capture"}:
        raise ValueError("恢复需要原因和已确认发送的未完成题目；发送不确定时只允许 resume 只读确认")
    if state.get("serial") != device.serial:
        raise ValueError("恢复设备与批次不一致")
    if action not in {"retry-current", "reenter-answer", "restart-app", "recapture-body"}:
        raise ValueError("未知恢复动作")
    if state.get("products_resume") and action != "retry-current":
        raise ValueError("药品终止阶段只允许原位置继续，不能关闭、重启或重新扫描正文")
    if state.get("mode") == "current" and action == "restart-app":
        raise ValueError("当前回答模式不允许重启应用")
    platform = PLATFORMS[task["entry"]]
    if action == "reenter-answer" and not platform.REENTER_SUPPORTED:
        raise ValueError("重新进入仅支持保留原题的平台搜索结果")
    package = PACKAGES[task["entry"]]
    engine = StepEngine(device, store, run_dir, state)
    observation = engine.observe()
    if observation.package != package:
        raise DeviceError("恢复前前台应用不匹配")
    if action == "recapture-body":
        body = state.get("body_resume") or {}
        if (task["entry"] != "douyin-direct" or task["phase"] != "capture" or state.get("inflight")
                or state.get("exploration", {}).get("dispatched") or not platform.answer_ready(observation)
                or (body.get("entry"), body.get("index"), body.get("question")) != (task["entry"], task["index"], task["text"])
                or not platform.submission_evidence(task, run_dir)):
            raise ValueError("重新采集正文需要当前直接提问的回答页、原始单次发送证据及匹配的正文断点")
        debug = Path(run_dir).resolve() / "调试产物" / task["entry"] / f"{task['index']:03d}"
        attempt = Path(body.get("attempt", "")).resolve()
        if attempt.parent != debug or not attempt.name.startswith("capture-") or not attempt.is_dir():
            raise ValueError("原始正文断点目录不属于本题")
    state.update(entry=task["entry"], index=task["index"])
    pending = state.get("pending_request")
    if pending:
        value = read_json(Path(pending))
        if (value.get("uncertain_send") or value.get("uncertain_action") or state.get("exploration", {}).get("dispatched")
                or not value["step"].startswith(("capture.", "layout.", "network.", "products.images_wait"))):
            raise ValueError("当前断点存在未确认操作，必须先 resume 验证，不能丢弃")
        value.update(status="superseded_by_recovery", finished_at=time.time())
        write_json(Path(pending), value)
        state.pop("pending_request", None)
    record = {"action": action, "reason": reason, "entry": task["entry"], "index": task["index"], "at": time.time(), "status": "started"}
    state.setdefault("recovery_actions", []).append(record)
    engine.log("recovery_started", action=action, reason=reason, submission_replayed=False)
    if action != "retry-current" and state.get("body_resume"):
        prior = state.pop("body_resume")
        write_json(Path(prior["attempt"]) / "superseded-checkpoint.json", prior)
        engine.log("body_checkpoint_archived", action=action, reason="重新进入后重新确认原题，保留旧帧但不跨会话拼接")
    if action != "retry-current":
        state.pop("capture_preparation", None)
        trace = state.pop("exploration", None)
        if trace:
            record["superseded_exploration"] = trace
    engine.save()
    if action == "reenter-answer":
        _phase(engine, task, "submitted")
        platform.reenter_answer(engine, task, observation)
    elif action == "restart-app":
        device.launch(package, restart=True)
    record["status"] = "dispatched"
    task["status"], state["status"] = "pending", "running"
    engine.log("recovery_dispatched", action=action, submission_replayed=False)
    engine.save()


def run_questions(device, store, run_dir, state, wait=0, teach=()):
    run_dir = Path(run_dir).resolve()
    tasks = state.get("tasks", [])
    if not tasks or any(task.get("entry") not in PACKAGES or isinstance(task.get("index"), bool) or not isinstance(task.get("index"), int) or task["index"] < 1
                        or not isinstance(task.get("text"), str) or not task["text"].strip() or task.get("phase", "start") not in PHASES for task in tasks):
        raise ValueError("任务必须包含支持的入口、明确题号、完整题目和有效断点。")
    if state.get("mode") == "current" and len(tasks) != 1:
        raise ValueError("当前回答采集必须明确指定一道题和一个入口。")
    if len({(task["entry"], task["index"]) for task in tasks}) != len(tasks):
        raise ValueError("入口和题号不能重复，否则会覆盖既有产物。")
    if isinstance(state.get("cursor", 0), bool) or not isinstance(state.get("cursor", 0), int) or not 0 <= state.get("cursor", 0) <= len(tasks):
        raise ValueError("任务游标不合法。")
    if state.get("serial") and state["serial"] != device.serial:
        raise ValueError("不能在另一台手机恢复当前问题。")
    # Older incomplete checkpoints may have advanced past a failed task. Resume
    # that task's existing phase; capture still has to prove its exact question.
    unfinished = next((index for index, task in enumerate(tasks) if task.get("status") != "complete"), len(tasks))
    state["cursor"] = min(state.get("cursor", 0), unfinished)
    engine = StepEngine(device, store, run_dir, state, wait=wait, teach=teach)
    for cursor in range(state.get("cursor", 0), len(tasks)):
        task = tasks[cursor]
        if task.get("status") == "complete":
            continue
        state.update(cursor=cursor, status="running")
        task["status"] = "running"
        _phase(engine, task, task.get("phase", "start"))
        package = PACKAGES[task["entry"]]
        platform = PLATFORMS[task["entry"]]
        debug = run_dir / "调试产物" / task["entry"] / f"{task['index']:03d}"
        delivery = run_dir / "交付图片" / task["entry"] / f"{task['index']:03d}"
        try:
            if state.get("mode") == "current" and not (task["phase"] == "capture" and any(
                    engine.goal_pending(step) for step in ("capture.question_top", "capture.review"))):
                current = engine.observe()
                entry_result = None
                if platform.can_enter_current(current, task) or (
                        platform.ENTRY_STEP and engine.state.get("exploration", {}).get("step") == platform.ENTRY_STEP):
                    entry_result = _enter_answer(engine, task, debug)
                    if entry_result is None:
                        current = engine.observe()
                if entry_result is not None:
                    _finish_entry_outcome(engine, task, debug, entry_result)
                elif current.package == package and not platform.answer_ready(current) and engine.teach & {"page.perception", "page.perception." + task["entry"]}:
                    current = engine.perception(current, "当前回答页Activity变化：绑定answer_page品牌锚点，仍需核对小荷品牌与完整原题；不可将视频首页当作回答")
                if entry_result is None and (current.package != package or not platform.answer_ready(current)):
                    raise DeviceError("当前回答模式需位于对应回答页；搜索平台必须已打开小荷小程序。未执行UI操作。")
                if entry_result is None:
                    _phase(engine, task, "capture")
            if task["phase"] == "submitting":
                observation = engine.observe()
                if callable(confirm := getattr(platform, "confirm_submission", None)):
                    observation = confirm(engine, task, observation)
                if not _sent(observation, task):
                    _uncertain(engine, task, observation, "断点停在发送中，当前现场不能证明完整原题已发送")
                _resolve_readonly_request(engine)
                engine.log("submission_confirmed_without_replay")
                _phase(engine, task, "submitted")
            if task["phase"] == "start":
                device.launch(package, restart=False)
                _phase(engine, task, "open")
            if task["phase"] == "open":
                _open(engine, task)
                _phase(engine, task, "input")
            if task["phase"] == "input":
                _input_and_send(engine, task)
            if task["phase"] == "submitted":
                entry_result = _enter_answer(engine, task, debug)
                if entry_result is None:
                    _phase(engine, task, "capture")
                else:
                    _finish_entry_outcome(engine, task, debug, entry_result)
            if task["phase"] == "capture":
                result = AnswerCapture(device, engine, answer_page_check=platform.answer_ready,
                                       answer_page_error=platform.ANSWER_PAGE_ERROR).run(task["text"], package, delivery, debug)
                if any(result.get(key) is not True for key in ("question_confirmed", "citations_checked", "completion_confirmed", "end_confirmed", "occlusion_checked")):
                    raise CaptureError("采集返回缺少正文完整性证据，不能标记完成。")
                if result.get("citations_found") and result.get("citations_expanded") is not True:
                    raise CaptureError("引用资料尚未展开，不能标记完成。")
                frame_count = result.get("frame_count")
                seams = result.get("seams", [])
                if type(frame_count) is not int or frame_count < 1 or len(seams) != frame_count - 1 or any(seam.get("verified") is not True for seam in seams):
                    raise CaptureError("正文接缝证据缺失或不连续。")
                products = result.get("products", {})
                if products.get("complete") is not True or (products.get("present") and any(products.get(key) is not True for key in ("first_item_confirmed", "end_confirmed", "images_ready", "seams_verified"))):
                    raise CaptureError("参考药品尚未完整采集。")
                if products.get("horizontal", {}).get("complete") is False:
                    raise CaptureError("参考药品横向规格尚未完整采集。")
                images = result.get("image_paths", [])
                if not images or any(Path(file).suffix != ".png" or not Path(file).resolve().is_relative_to(delivery) or not Path(file).is_file() for file in images):
                    raise CaptureError("正式PNG图片缺失或不在本题交付目录内。")
                result.update(entry=task["entry"], index=task["index"], question=task["text"], status="complete",
                              outcome="captured", goal_contract_version=CONTRACT["version"])
                write_json(debug / "answer.json", result)
                engine.commit_flows(result)
                task.update(status="complete", outcome="captured", answer=str(debug / "answer.json"))
                _phase(engine, task, "done")
                engine.log("question_complete", answer=task["answer"])
        except NeedsRepair as error:
            task["status"] = "needs_repair"
            state["status"] = "needs_repair"
            _save_failure(engine, task, debug, error)
            engine.save()
            raise
        except Exception as error:
            _save_failure(engine, task, debug, error)
            if task["phase"] == "submitting":
                try:
                    _uncertain(engine, task, engine.observe(), "发送过程异常，仅允许只读确认，不重放")
                except NeedsRepair:
                    task["status"] = "needs_repair"
                    state["status"] = "needs_repair"
                    engine.save()
                    raise
                # Even if diagnostics are unavailable, never advance to another send.
                state["status"] = "incomplete"
                engine.save()
                raise
            task.update(status="failed", failure=str(debug / "failure.json"))
            state.pop("inflight", None)
            state.pop("pending_request", None)
            state["status"] = "incomplete"
            engine.log("question_incomplete", error=str(error), failure=task["failure"])
            engine.save()
            return state
        state["cursor"] = cursor + 1
        engine.save()
    state["cursor"] = len(tasks)
    state["status"] = "complete" if all(task.get("status") == "complete" for task in tasks) else "incomplete"
    state["finished_at"] = time.time()
    engine.save()
    return state
