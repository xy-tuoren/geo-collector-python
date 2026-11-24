from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .images import crop, stable
from .rules import UI_LABELS, LocateError, apply_layout, infer_target, locate, present, validate_recipe
from .goals import BEFORE_SEND, CONTRACT, bind_roles, guard_matches, page_guard, prune_route, repair_context
from .storage import event, read_json, write_json


class NeedsRepair(RuntimeError):
    def __init__(self, request):
        self.request = str(request)
        super().__init__(f"需要 Agent 修复：{request}")


class StepSatisfied(Exception):
    def __init__(self, observation):
        self.observation = observation


def save_observation(directory, observation):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "screen.png").write_bytes(observation.png)
    (directory / "screen.xml").write_text(observation.xml, encoding="utf-8")
    value = asdict(observation)
    value.pop("png")
    write_json(directory / "observation.json", value)


def load_observation(directory):
    from .device import Observation
    directory = Path(directory)
    value = read_json(directory / "observation.json")
    value["png"] = (directory / "screen.png").read_bytes()
    value["logical_size"] = tuple(value["logical_size"])
    value["physical_size"] = tuple(value["physical_size"])
    return Observation(**value)


def unchanged_control(original, current, recipe, assets):
    """A playing video may change while a named navigation control stays put.

    Only the first action is dispatched against the original scene; later
    actions are located again on the page they produce, so only that control
    (or, for a system back, the page's structural anchors) has to be unchanged.
    """
    actions = recipe.get("actions", [])
    if not actions or original.activity != current.activity:
        return False
    action = actions[0]
    if action["type"] == "back":
        return guard_matches(page_guard(original), current) and guard_matches(page_guard(current), original)
    if "target" not in action:
        return False
    target = action["target"]
    kind = target["kind"]
    approved_ocr = kind == "ocr" and sum(bool(re.fullmatch(target["pattern"], label)) for label in UI_LABELS) == 1
    if ((kind == "node" and target["attr"] == "class_name") or (kind != "node" and not approved_ocr)
            or not all(0 <= n <= 1 for n in target.get("offset", [.5, .5]))):
        return False
    try:
        points = [locate(target, item, assets) for item in (original, current)]
    except LocateError:
        return False
    size = original.logical_size
    if any(abs(a - b) > size[i] * .01 for i, (a, b) in enumerate(zip(*points))):
        return False
    routes = []
    for item, (x, y) in zip((original, current), points):
        nodes = [n for n in item.nodes if n["bounds"][0] <= x < n["bounds"][2] and n["bounds"][1] <= y < n["bounds"][3]]
        local = [n for n in nodes if n.get("enabled", True)
                 and (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]) < size[0] * size[1] * .15]
        # A whitelisted label or a stable resource id names a control; free text may be content.
        named = any((n.get("text") or n.get("description")) in UI_LABELS
                    or (target.get("attr") == "resource_id" and n.get("resource_id") == target.get("value"))
                    for n in local)
        if not local or (kind == "node" and not named):
            return False
        routes.append(nodes if kind == "ocr" else [n for n in nodes if n.get("clickable")
                      or (n.get("text") or n.get("description")) in UI_LABELS
                      or n.get(target["attr"]) == target["value"]])
    if len(routes[0]) != len(routes[1]):
        return False
    attrs = ("package", "class_name", "resource_id", "text", "description", "enabled", "clickable")
    return all(all(a.get(key) == b.get(key) for key in attrs)
               and all(abs(x - y) <= size[i % 2] * .01 for i, (x, y) in enumerate(zip(a["bounds"], b["bounds"])))
               for a, b in zip(*routes))


def check_exploration(action, point, observation, state):
    """The same side-effect checks for an offline proposal and the live dispatch."""
    if action["type"] == "back" and (state.get("mode") == "current" or state.get("products_resume")):
        raise LocateError("当前回答和药品终止阶段不允许探索系统返回")
    if point:
        labels = [str(n.get("text") or n.get("description") or "") for n in observation.nodes
                  if n["bounds"][0] <= point[0] < n["bounds"][2] and n["bounds"][1] <= point[1] < n["bounds"][3]
                  and (n["bounds"][2]-n["bounds"][0]) * (n["bounds"][3]-n["bounds"][1]) < observation.logical_size[0]*observation.logical_size[1]*.2]
        px = point[0] * observation.physical_size[0] / observation.logical_size[0]
        py = point[1] * observation.physical_size[1] / observation.logical_size[1]
        labels += [line["text"].strip() for line in observation.texts if line.get("confidence", 0) >= .78
                   and line["bounds"][0] <= px < line["bounds"][2] and line["bounds"][1] <= py < line["bounds"][3]]
        if any(re.fullmatch(r"发送|提交|去购买|购买|立即购买|支付|删除.*", label) for label in labels):
            raise LocateError("探索不能执行发送、购买或删除操作")
        readonly = state.get("mode") == "current" or state.get("phase") in {"submitted", "capture"}
        if readonly and any(re.fullmatch(r"搜索|开启新会话|新会话|输入问题.*|发送消息|输入消息", label) for label in labels):
            raise LocateError("已提交题目不能通过探索重新搜索或新建会话")
        if readonly and action["type"] == "tap" and any(n.get("class_name", "").endswith("EditText")
                and n["bounds"][0] <= point[0] < n["bounds"][2] and n["bounds"][1] <= point[1] < n["bounds"][3] for n in observation.nodes):
            raise LocateError("已有回答模式不能聚焦输入框")
        if any(n.get("class_name", "").endswith("EditText") and n.get("focused") and n.get("text") for n in observation.nodes) and "搜索" in labels:
            raise LocateError("搜索提交由原题提交器执行")


def _repair_scene(request_file, sha256, *, archived=False):
    request = read_json(request_file)
    if not archived and request.get("status") != "pending":
        raise ValueError("该请求已经结束，不能提交旧修复")
    observation = load_observation(request_file.parent)
    if sha256 != request["sha256"] or hashlib.sha256(observation.png).hexdigest() != sha256:
        raise ValueError("修复必须关联刚刚查看的原始截图 SHA256")
    return request, observation


def validate_repair_plan(request, observation, plan, *, skip_locate=False):
    validate_recipe(plan)
    if "interrupt" in plan:
        raise ValueError("中断规则需要 teach interrupt 验证实际关闭效果")
    if request.get("package") and observation.package != request["package"]:
        raise ValueError("原始现场前台应用与请求不匹配")
    if request.get("readonly") and ("perception" not in plan or request.get("step", "").endswith(".results_ready")):
        raise ValueError("完整性复查仅接受页面角色；等待后resume或使用请求列出的recover命令，不能直接移动已采集位置")
    if "route" in plan:
        raise ValueError("完整路径只能由实际逐步探索生成，不能直接提交未执行的路线")
    if "perception" in plan:
        if request.get("uncertain_send") or request.get("uncertain_action"):
            raise ValueError("操作结果不确定，必须先只读确认")
        bind_roles(plan, observation, require=True)
    elif request.get("step") == "page.perception":
        raise ValueError("页面识别请求需要perception角色方案")
    if ("layout" in plan) != request.get("step", "").startswith("layout."):
        raise ValueError("布局方案只能用于布局请求，操作请求必须使用动作方案")
    if request.get("uncertain_send") or request.get("uncertain_action"):
        raise ValueError("操作结果不确定，不能用修复动作重放；先只读检查当前状态")
    if (plan.get("explore") or plan.get("transient")) and (request.get("step", "").endswith(".send") or request.get("readonly")):
        raise ValueError("发送和只读完整性检查不允许探索动作")
    if not skip_locate and "actions" in plan and "target" in plan["actions"][0]:
        # Later actions may target a page that only exists after the first action.
        try:
            locate(plan["actions"][0]["target"], observation, Path(request["rule_assets"]))
        except LocateError as error:
            raise ValueError(f"修复首步在原始现场无法定位：{error}；请修正定位条件后重新提交同一请求") from error


def submit_repair(request_file, *, point=None, plan=None, sha256=None, candidate=None, role=None, preview=False):
    from .repair_preview import candidate_plan, preview_plan
    request_file = Path(request_file).resolve()
    request, observation = _repair_scene(request_file, sha256, archived=preview)
    if role is not None and candidate is None:
        raise ValueError("--role 需要 --candidate")
    if sum(x is not None for x in (point, plan, candidate)) > 1:
        raise ValueError("point 和 plan 只能提供一个，或使用 candidate")
    if candidate is not None:
        try:
            plan = candidate_plan(request, observation, candidate, role)
        except ValueError as error:
            if not preview:
                raise
            result = preview_plan(request, observation)
            return {**result, "scene_check": "rejected", "missing_evidence": [str(error)],
                    "next": "resume" if result["allowed_resolutions"] == ["resume"] else "revise_or_inspect"}
    point_override = None
    if point is not None:
        try:
            target = infer_target(observation, point, Path(request["rule_assets"]), request.get("hints", ()), save_assets=not preview)
            plan = {"actions": [{"type": "tap", "target": target}]}
            if preview:
                point_override = (point[0] * observation.logical_size[0] / observation.physical_size[0],
                                  point[1] * observation.logical_size[1] / observation.physical_size[1])
        except (ValueError, LocateError) as error:
            if not preview:
                raise
            result = preview_plan(request, observation)
            return {**result, "scene_check": "rejected", "missing_evidence": [str(error)],
                    "next": "resume" if result["allowed_resolutions"] == ["resume"] else "revise_or_inspect"}
    if preview:
        result = preview_plan(request, observation, plan, point_override=point_override)
        if request.get("status") != "pending":
            result["next"] = "archived_request"
        return result
    if plan is None:
        raise ValueError("提交修复需要 --plan、--candidate 或 --point")
    validate_repair_plan(request, observation, plan)
    response = {"request_id": request["request_id"], "sha256": sha256, "recipe": plan}
    destination = request_file.parent / "response.json"
    # A repair response is written once; the engine promotes it only after verification.
    temporary = destination.with_name(f".response-{uuid.uuid4().hex}.json")
    try:
        write_json(temporary, response)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return response


class StepEngine:
    def __init__(self, device, store, run_dir, state, *, wait=0, teach=()):
        self.device, self.store = device, store
        self.run_dir, self.state = Path(run_dir), state
        self.wait = wait
        self.teach = set(teach)
        self.layout_used = set()
        self.perception_used = set()
        self.log_file = self.run_dir / "调试产物" / "events.jsonl"

    def save(self):
        self.state["teach"] = sorted(self.teach)
        write_json(self.run_dir / "调试产物" / "run.json", self.state)

    def log(self, name, **details):
        event(self.log_file, name, entry=self.state.get("entry"), question_index=self.state.get("index"), **details)

    def observe(self):
        return self.decorate(self.device.observe(ocr=True))

    def goal_pending(self, step):
        pending = self.state.get("pending_request")
        return bool((pending and read_json(Path(pending)).get("step") == step)
                    or self.state.get("exploration", {}).get("step") == step)

    def prefer_goal(self, step, observation):
        """Use applicable Agent methods before the default navigation strategy."""
        if step in self.teach or self.goal_pending(step):
            return True
        pending = self.state.get("flow_candidates", {}).get(step)
        candidates = ([pending] if pending and pending["context"] == self._context(observation) else []) + self.store.candidates(observation.package, step, observation)
        for item in candidates:
            recipe = item["recipe"]
            try:
                if "route" in recipe:
                    if not guard_matches(recipe["route"][0]["before"], observation):
                        continue
                    action = recipe["route"][0]["action"]
                else:
                    action = recipe["actions"][0]
                if "target" in action:
                    locate(action["target"], observation, self.store.assets)
                return True
            except (LocateError, ValueError, OSError):
                pass
        return False

    def review_capture(self, observation):
        pending = self.state.get("pending_request")
        if not pending or read_json(Path(pending)).get("step") != "capture.review":
            return observation
        file = Path(pending)
        request = read_json(file)
        if (file.parent / "response.json").exists():
            recipe, current, file = self._request("capture.review", request["goal"], observation, (), request["reason"])
            if "perception" not in recipe:
                raise ValueError("采集复查只能重新识别页面角色，不能替代完整性验收")
            return self._accept_perception(recipe, current, file)
        self.resolve_capture_request("capture.review")
        return observation

    def decorate(self, observation):
        from .goals import ROLES
        if self.state.get('entry') == 'douyin-direct' and self.state.get('phase') in {'open', 'input', 'submitting'}:
            task = next((t for t in self.state.get('tasks', []) if (t['entry'], t['index']) == ('douyin-direct', self.state.get('index'))), {})
            if signature := task.get('direct_open', {}).get('reset_welcome'):
                observation = replace(observation, reset_welcome=signature)
        for role in sorted(ROLES):
            if role in {"session_welcome", "session_divider"} and self.state.get("entry") != "douyin-direct":
                continue
            key = "perception." + role
            pending = self.state.get("flow_candidates", {}).get(key)
            candidates = ([pending] if pending and pending["context"] == self._context(observation) else []) + self.store.candidates(observation.package, key, observation)
            for item in candidates:
                try:
                    result = bind_roles(item["recipe"], observation)
                    if any(n.get("collector_role") == role for n in result.nodes):
                        observation = result
                        if item is pending:
                            self.perception_used.add(key)
                            item["used"] = True
                        break
                except (ValueError, RuntimeError):
                    continue
        return observation

    def _reject_binding(self, file, observation, error):
        """A read-only hypothesis failed on resume. Preserve it and hand back a fresh scene."""
        previous = read_json(file)
        previous.update(status="rejected", error=str(error), finished_at=time.time())
        write_json(file, previous)
        self.state.pop("pending_request", None)
        self.save()
        self.log("binding_rejected", step=previous["step"], reason=str(error))
        self._request(previous["step"], previous["goal"], observation, previous.get("hints", []),
                      f"上次识别方案未通过现场复验：{error}。可在新现场预演其他方案", hand_back=True)

    def _accept_perception(self, recipe, observation, file):
        try:
            bind_roles(recipe, observation, require=True)
        except (ValueError, LocateError) as error:
            self._reject_binding(file, observation, error)
        for role, target in recipe["perception"].items():
            key = "perception." + role
            self.state.setdefault("flow_candidates", {})[key] = {
                "recipe": {"perception": {role: target}}, "context": self._context(observation), "request": str(file)}
        self._close_request(file, "candidate")
        self.log("page_roles_learned", roles=list(recipe["perception"]), reusable=False)
        return self.decorate(observation)

    def perception(self, observation, reason):
        recipe, current, file = self._request("page.perception", "根据原图绑定问题、引用、药品或输入区域；保留原始文字，由原有完整性检查验收", observation, (), reason)
        if "perception" not in recipe:
            raise ValueError("页面识别请求必须提供perception")
        return self._accept_perception(recipe, current, file)

    def _close_request(self, file, status):
        if file:
            value = read_json(Path(file))
            value.update(status=status, finished_at=time.time())
            write_json(Path(file), value)
        self.state.pop("pending_request", None)
        self.state["status"] = "running"
        self.save()

    def commit_flows(self, result=None):
        from types import SimpleNamespace
        for step, item in self.state.pop("flow_candidates", {}).items():
            if step.startswith("perception.") and not item.get("used"):
                continue
            if step == "perception.session_welcome" and (result or {}).get("direct_submission", {}).get("session_evidence") != "welcome":
                continue
            if step == "perception.session_divider" and (result or {}).get("direct_submission", {}).get("draft_evidence") != "aligned_divider":
                continue
            if result is not None:
                if step == "perception.question" and result.get("question_confirmation_method") != "learned_question_role_and_exact_ocr":
                    continue
                if step in {"perception.citations_header", "perception.citations_rows"} and result.get("citation_confirmation_method") != "learned_roles_with_visible_reference_text":
                    continue
                if step == "perception.products_header" and not (result.get("products") or {}).get("present"):
                    continue
                if step == "perception.products_entry" and (result.get("products") or {}).get("mode") != "drawer":
                    continue
            context = SimpleNamespace(**item["context"])
            self.store.promote(context.package, step, item["recipe"], context, "agent")
            if item.get("request"):
                value = read_json(Path(item["request"]))
                value.update(status="verified", finished_at=time.time())
                write_json(Path(item["request"]), value)
            self.log("flow_rule_promoted", step=step, capture_complete=True)
        pending_lessons = self.state.pop("lesson_candidates", [])
        recalled_lessons = self.state.pop("recalled_lessons", [])
        if pending_lessons or recalled_lessons:
            from .lessons import LessonStore
            lesson_store = LessonStore(self.store.root)
            for spec in pending_lessons:
                saved = lesson_store.add(spec)
                self.log("lesson_promoted", lesson_id=saved["id"], strategy=saved["strategy"])
            for lid in recalled_lessons:
                lesson_store.record_outcome(lid, success=True)
        self.save()

    def resolve_capture_request(self, step):
        pending = self.state.get("pending_request")
        if pending:
            file = Path(pending)
            value = read_json(file)
            if (value.get("step"), value.get("entry"), value.get("question_index")) == (step, self.state.get("entry"), self.state.get("index")):
                value.update(status="rechecking" if step == "capture.review" else "satisfied_without_replay", finished_at=time.time())
                write_json(file, value)
                self.state.pop("pending_request", None)
                self.save()

    def layout(self, role, observation, check, *, request=False):
        """Try guarded local hints; candidates only become reusable after capture."""
        step = "layout." + role
        package = observation.package
        pending = self.state.get("layout_candidates", {}).get(step)
        saved = self.store.candidates(package, step, observation)
        candidates = ([pending] if pending and pending["context"] == self._context(observation) else []) + saved
        for candidate in candidates:
            try:
                result = check(apply_layout(candidate["recipe"], observation))
                if result:
                    if candidate is pending:
                        self.layout_used.add(step)
                        candidate["used"] = True
                    self.log("layout_rule_used", step=step, candidate=candidate is pending)
                    return result
            except (ValueError, RuntimeError):
                pass
        native = check(observation)
        if native or not request:
            if native:
                self.resolve_capture_request(step)
            return native
        recipe, current, file = self._request(step, "识别真实内容容器或药品照片；不能缩小完整性要求，不能操作手机", observation, (),
                                               "现有布局无法验证；提供有节点锚点的 layout 方案")
        try:
            if recipe.get("layout", {}).get("role") != role:
                raise ValueError("布局方案角色与当前失败阶段不符")
            result = check(apply_layout(recipe, current))
            if not result:
                raise ValueError("布局修复未通过原始检查")
        except (ValueError, RuntimeError) as error:
            self._reject_binding(file, current, error)
        self.state.setdefault("layout_candidates", {})[step] = {"recipe": recipe, "context": self._context(current), "request": str(file), "used": True}
        self.layout_used.add(step)
        value = read_json(file)
        value.update(status="candidate", finished_at=time.time())
        write_json(file, value)
        self.state.pop("pending_request", None)
        self.state["status"] = "running"
        self.save()
        return result

    @staticmethod
    def _context(observation):
        return {"package": observation.package, "version": observation.version, "activity": observation.activity,
                "logical_size": list(observation.logical_size)}

    def commit_layouts(self):
        from types import SimpleNamespace
        for step, item in self.state.pop("layout_candidates", {}).items():
            context = SimpleNamespace(**item["context"])
            value = read_json(Path(item["request"]))
            used = item.get("used") or step in self.layout_used
            if used:
                self.store.promote(context.package, step, item["recipe"], context, "agent")
                self.log("layout_rule_promoted", step=step, capture_complete=True)
            value.update(status="verified" if used else "unused", finished_at=time.time())
            write_json(Path(item["request"]), value)
        self.save()

    def _request(self, step, goal, observation, hints, reason, *, uncertain_send=False, uncertain_action=False, stale_count=0, postcondition=None, hand_back=False):
        old = self.state.get("pending_request")
        request_file = Path(old) if old else None
        if request_file and request_file.exists():
            request = read_json(request_file)
            if request["step"] != step or request["question_index"] != self.state["index"] or request["entry"] != self.state["entry"]:
                raise RuntimeError("断点的步骤或题目身份不匹配")
        else:
            directory = self.run_dir / "调试产物" / "repairs" / uuid.uuid4().hex
            save_observation(directory, observation)
            request_file = directory / "request.json"
            request = {"version": 1, "request_id": directory.name, "status": "pending", "step": step, "goal": goal,
                       "reason": str(reason), "entry": self.state["entry"], "question_index": self.state["index"],
                       "package": observation.package, "hints": list(hints), "uncertain_send": uncertain_send,
                       "uncertain_action": uncertain_action,
                       "sha256": hashlib.sha256(observation.png).hexdigest(), "screenshot": str(directory / "screen.png"),
                       "observation": str(directory / "observation.json"), "run_directory": str(self.run_dir),
                       "rule_assets": str(self.store.assets), "coordinate_space": "physical_pixels",
                       "contract": CONTRACT, "phase": self.state.get("phase"), "mode": self.state.get("mode"),
                       "readonly": step in {"capture.review", "products.images_wait", BEFORE_SEND} or step.endswith(".results_ready"),
                       "explored_steps": len(self.state.get("exploration", {}).get("route", [])),
                       "instructions": "先运行 handoff，再用 repair --preview 在原图试验候选并查看缺少的证据。按 allowed_resolutions 恢复原批次。导航目标优先用 `geo-collect teach <run> tap|swipe|back` 逐步驱动手机（每次一个已观察动作，CLI记录页面证据，不需要response.json），或提交perception；只读复查不可移动已采集位置。达到目标后 resume 重新验收，整题通过才保存采集路径。输入与发送由CLI管理。",
                       "teach": f"geo-collect teach {self.run_dir} status",
                       **repair_context(self.state, step, reason)}
            if uncertain_send or uncertain_action:
                request["allowed_resolutions"] = ["resume"]
            write_json(request_file, request)
            self.state.update(status="needs_repair", pending_request=str(request_file))
            self.state["agent_requests"] = self.state.get("agent_requests", 0) + 1
            self.save()
            self.log("repair_requested", request=str(request_file), step=step, reason=str(reason))
        if hand_back:
            raise NeedsRepair(request_file)
        response_file = request_file.parent / "response.json"
        deadline = time.monotonic() + self.wait
        while not response_file.exists() and time.monotonic() < deadline:
            if postcondition:
                current = self.observe()
                if current.package == observation.package and postcondition(current):
                    raise StepSatisfied(current)
            time.sleep(.2)
        if not response_file.exists():
            raise NeedsRepair(request_file)
        response = read_json(response_file)
        if response.get("request_id") != request["request_id"] or response.get("sha256") != request["sha256"]:
            raise ValueError("修复响应属于另一份现场，已拒绝")
        recipe = validate_recipe(response["recipe"])
        if uncertain_send or uncertain_action or request.get("uncertain_send") or request.get("uncertain_action"):
            raise NeedsRepair(request_file)
        if request.get("readonly") and ("perception" not in recipe or request.get("step", "").endswith(".results_ready")):
            raise ValueError("只读完整性复查不能执行动作")
        original = load_observation(request_file.parent)
        if hashlib.sha256(original.png).hexdigest() != request["sha256"]:
            raise ValueError("修复现场截图已改变，拒绝使用旧响应")
        current = self.observe()
        if postcondition and current.package == original.package and postcondition(current):
            raise StepSatisfied(current)
        pw, ph = original.physical_size
        region = [0, ph * .10, pw, ph * .88]
        same_window = (current.package == original.package and current.activity == original.activity
                       and current.logical_size == original.logical_size and current.physical_size == original.physical_size)
        pixels_unchanged = same_window and stable(crop(original.png, region), crop(current.png, region))
        # Page roles and layouts are bound on the current observation, so a changed
        # background cannot make them act on the wrong control.
        control_unchanged = same_window and ("perception" in recipe or "layout" in recipe
                                             or (postcondition is not None and unchanged_control(original, current, recipe, self.store.assets)))
        if not (pixels_unchanged or control_unchanged):
            request.update(status="stale", finished_at=time.time())
            write_json(request_file, request)
            self.state.pop("pending_request", None)
            self.save()
            if stale_count >= 2:
                raise RuntimeError("修复期间页面持续变化，暂停等待页面稳定")
            return self._request(step, goal, current, hints, "恢复前页面发生变化，请根据新现场定位", stale_count=stale_count + 1, postcondition=postcondition)
        if not pixels_unchanged:
            self.log("repair_control_revalidated", step=step, background_changed=True)
        self.state.update(status="running")
        self.save()
        return recipe, current, request_file

    def clear_interrupts(self, observation, package, *, limit=2):
        """Dismiss learned dialogs/overlays before a step, without touching sends.

        Each interrupt rule pairs a detect target with 1–3 dismiss actions. It is
        applied only while its target is visible, verified by the target
        disappearing, and counted like any other rule when it fails.
        """
        if not hasattr(self.store, "interrupts"):
            return observation
        for _ in range(limit):
            matched = None
            for step, rule in self.store.interrupts(package, observation):
                if present(rule["recipe"]["interrupt"]["detect"], observation):
                    matched = (step, rule)
                    break
            if matched is None:
                return observation
            step, rule = matched
            detail = rule["recipe"]["interrupt"]
            self.log("interrupt_detected", step=step, revision=rule.get("revision"))
            before = observation
            try:
                for index, action in enumerate(detail["actions"]):
                    if index:
                        observation = self.observe()
                        if observation.package != package:
                            raise LocateError("关闭中断期间前台应用改变")
                    self._act(action, observation, package, exploration=True)
                    time.sleep(.35)
                observation = self.observe()
                if observation.package != package or present(detail["detect"], observation):
                    raise LocateError("中断关闭后目标仍然可见")
            except (LocateError, RuntimeError, OSError) as error:
                self.store.invalidate(package, step, str(error), rule["revision"], before.version)
                self.log("interrupt_dismiss_failed", step=step, error=str(error))
                return observation if observation.package == package else before
            self.store.record_success(package, step, rule["revision"], before)
            self.log("interrupt_dismissed", step=step)
        return observation

    def _already_satisfied(self, step, observation):
        self.state.pop("inflight", None)
        pending = self.state.get("pending_request")
        if pending:
            request = read_json(Path(pending))
            if request["step"] == step:
                request.update(status="satisfied_without_replay", finished_at=time.time())
                write_json(Path(pending), request)
                self.state.pop("pending_request", None)
        self.state["status"] = "running"
        self.save()
        self.log("step_already_satisfied", step=step)
        return observation

    def _act(self, action, observation, package, *, exploration=False, before_dispatch=None):
        """One action dispatcher for normal rules, exploration and recorded replay."""
        point = locate(action["target"], observation, self.store.assets) if "target" in action else None
        if exploration:
            check_exploration(action, point, observation, self.state)
        if before_dispatch:
            before_dispatch()
        if action["type"] == "tap":
            self.device.tap(point, expected_package=package)
        elif action["type"] == "back":
            self.device.press("back", expected_package=package)
        else:
            w, h = observation.logical_size
            start, end = ((w * .5, h * .72), (w * .5, h * .35))
            if action["direction"] == "up":
                start, end = end, start
            if point:
                start = point
                delta = h * action.get("distance_fraction", .37) * (1 if action["direction"] == "up" else -1)
                end = (start[0], max(h * .035, min(h * .94, start[1] + delta)))
            self.device.swipe(start, end, expected_package=package)

    def _explore(self, step, goal, package, postcondition, recipe, observation, file, *, before_action=None):
        transient = bool(recipe.get("transient"))
        trace = self.state.setdefault("exploration", {"step": step, "context": self._context(observation), "route": []})
        if trace["step"] != step:
            raise LocateError("探索断点与当前目标不一致")
        if trace.get("dispatched"):
            # A process may die after dispatch. Never guess whether the action ran.
            raise LocateError("探索动作结果不确定，先观察并使用只读resume确认目标；不能重放")
        if not transient and len(trace["route"]) >= 32:
            raise LocateError("单目标探索已达32步，保留证据，不能无限点击")
        action = recipe["actions"][0]
        hop = {"action": action, "before": page_guard(observation)}
        if before_action and not trace["route"]:
            before_action()
        def dispatching():
            trace["dispatched"] = hop
            self.save()
        self._act(action, observation, package, exploration=True, before_dispatch=dispatching)
        time.sleep(.35)
        after = self.observe()
        if after.package != package:
            raise LocateError("探索后离开目标应用，已停止")
        trace.pop("dispatched")
        if transient:
            if not trace["route"]:
                self.state.pop("exploration", None)
            self.state["transient_recovery_steps"] = self.state.get("transient_recovery_steps", 0) + 1
            self._close_request(file, "transient")
            self.log("transient_recovery_step", step=step, goal_satisfied=bool(postcondition(after)))
        else:
            hop["after"] = page_guard(after)
            trace["route"].append(hop)
            self.state["goal_exploration_steps"] = self.state.get("goal_exploration_steps", 0) + 1
            self._close_request(file, "explored")
            self.log("goal_exploration_step", step=step, ordinal=len(trace["route"]), goal_satisfied=bool(postcondition(after)))
        if postcondition(after):
            return self._finish_exploration(step, after)
        # Observe the resulting page before asking for another action. No 12s
        # final-goal timeout and no replay of the route prefix on resume.
        try:
            self._request(step, goal, after, (), "尚未达到目标，请查看新页面，提交下一步或页面角色；已执行步骤不会重放", postcondition=postcondition)
        except StepSatisfied as done:
            return self._finish_exploration(step, done.observation)
        return self.perform(step, goal, package, postcondition)

    def _finish_exploration(self, step, observation):
        trace = self.state.pop("exploration", None)
        if trace and trace["step"] == step and not trace.get("dispatched") and trace["route"]:
            pruned = prune_route(trace["route"])
            if pruned:
                self.state.setdefault("flow_candidates", {})[step] = {"recipe": {"route": pruned}, "context": trace["context"]}
                self.log("goal_route_learned", step=step, steps=len(pruned), reusable=False)
        self.teach.discard(step)
        return self._already_satisfied(step, observation)

    def _replay_route(self, step, package, recipe, observation, postcondition, before_action):
        trace = {"step": step, "context": self._context(observation), "route": []}
        self.state["exploration"] = trace
        try:
            for index, hop in enumerate(recipe["route"]):
                if postcondition(observation):
                    self.state.pop("exploration", None)
                    return observation
                if observation.package != package or not guard_matches(hop["before"], observation):
                    raise LocateError("已保存路径的中间页面变化")
                if index == 0 and before_action:
                    before_action()
                actual = {"action": hop["action"], "before": page_guard(observation)}
                def dispatching():
                    trace["dispatched"] = actual
                    self.state["inflight"] = {"step": step, "risk": "route", "before": time.time()}
                    self.save()
                self._act(hop["action"], observation, package, exploration=True, before_dispatch=dispatching)
                time.sleep(.35)
                observation = self.observe()
                if observation.package == package:
                    actual["after"] = page_guard(observation)
                    trace["route"].append(actual)
                    trace.pop("dispatched", None)
                    self.save()
                if observation.package != package or not guard_matches(hop["after"], observation):
                    raise LocateError("路径动作后的页面证据变化，停止剩余动作")
                self.state.pop("inflight", None)
                self.state["goal_route_replay_steps"] = self.state.get("goal_route_replay_steps", 0) + 1
                self.save()
                self.log("goal_route_replayed", step=step, ordinal=index + 1)
            deadline = time.monotonic() + 12
            while not postcondition(observation):
                if time.monotonic() >= deadline:
                    raise LocateError("路径结束后原目标未达成")
                time.sleep(.25)
                observation = self.observe()
                if observation.package != package:
                    raise LocateError("路径结束后离开目标应用")
            self.state.pop("exploration", None)
            return observation
        finally:
            if not trace.get("dispatched"):
                self.state.pop("inflight", None)
                if not trace["route"] and self.state.get("exploration") is trace:
                    self.state.pop("exploration", None)
                    self.save()

    def perform(self, step, goal, package, postcondition, *, defaults=(), hints=(), force=False, risk="tap", before_action=None):
        original_check = postcondition
        def postcondition(current):
            try:
                return original_check(current)
            except (ValueError, LocateError):
                return False
        observation = self.observe()
        if observation.package != package:
            raise RuntimeError(f"步骤 {step} 要求前台 {package}，实际 {observation.package}")
        if not force and postcondition(observation):
            return self._finish_exploration(step, observation)
        uncertain = self.state.get("inflight", {}).get("step") == step and risk in {"send", "toggle"}
        if not uncertain and not self.state.get("exploration", {}).get("dispatched"):
            cleared = self.clear_interrupts(observation, package)
            if cleared is not observation:
                observation = cleared
                if observation.package != package:
                    raise RuntimeError(f"步骤 {step} 要求前台 {package}，实际 {observation.package}")
                if not force and postcondition(observation):
                    return self._finish_exploration(step, observation)
        tried: set[tuple[str, int]] = set()

        def pick_candidate(current_obs):
            if self.state.get("pending_request") or self.state.get("exploration") or step in self.teach or uncertain:
                return None, "builtin", None, None
            pending = self.state.get("flow_candidates", {}).get(step)
            records = ([pending] if pending and pending["context"] == self._context(current_obs) else []) + self.store.candidates(package, step, current_obs)
            for idx, record in enumerate([*records, *({"recipe": r} for r in defaults)]):
                candidate_source = "saved" if "revision" in record else "candidate" if record is pending else "builtin"
                token = (candidate_source, record["revision"] if candidate_source == "saved" else idx)
                if token in tried:
                    continue
                tried.add(token)
                candidate = record["recipe"]
                try:
                    validate_recipe(candidate)
                    if "route" in candidate:
                        if risk == "send" or not guard_matches(candidate["route"][0]["before"], current_obs):
                            continue
                        first_hop = candidate["route"][0]["action"]
                        if first_hop["type"] == "tap" or "target" in first_hop:
                            locate(first_hop["target"], current_obs, self.store.assets)
                        return candidate, ("saved" if "revision" in record else "candidate"), record, None
                    first = candidate["actions"][0]
                    if first["type"] == "tap" or "target" in first:
                        locate(first["target"], current_obs, self.store.assets)
                    return (candidate, candidate_source,
                            record if candidate_source == "saved" else None,
                            record.get("request") if candidate_source == "candidate" else None)
                except (LocateError, ValueError, OSError):
                    if candidate_source == "saved":
                        self.store.invalidate(package, step, "缓存规则不能唯一定位", record["revision"], current_obs.version)
                    continue
            return None, "builtin", None, None

        recipe, source, learned, request_file = pick_candidate(observation)
        for attempt in range(3):
            action_before = None
            action_dispatched = False
            if recipe is None:
                try:
                    recipe, observation, request_file = self._request(step, goal, observation, hints, "当前规则不能唯一定位或后置检查失败", uncertain_send=uncertain and risk == "send", uncertain_action=uncertain and risk == "toggle", postcondition=None if force else postcondition)
                except StepSatisfied as done:
                    return self._finish_exploration(step, done.observation)
                source = "agent"
            if "perception" in recipe:
                self._accept_perception(recipe, observation, request_file)
                return self.perform(step, goal, package, postcondition, defaults=defaults, hints=hints, force=force, risk=risk, before_action=before_action)
            if recipe.get("explore") or recipe.get("transient") or (self.state.get("exploration") and "actions" in recipe):
                if risk == "send":
                    raise LocateError("发送步骤不允许探索")
                if len(recipe["actions"]) != 1:
                    raise LocateError("逐步探索需要一次一个动作")
                return self._explore(step, goal, package, postcondition, recipe, observation, request_file, before_action=before_action)
            self.log("step_started", step=step, source=source, revision=(learned or {}).get("revision"))
            rule_context = observation
            try:
                if "route" in recipe:
                    observation = self._replay_route(step, package, recipe, observation, postcondition, before_action)
                    if source == "saved" and learned:
                        self.store.record_success(package, step, learned["revision"], rule_context)
                    return self._already_satisfied(step, observation)
                if risk in {"send", "toggle"} and (len(recipe["actions"]) != 1 or recipe["actions"][0]["type"] != "tap"):
                    raise LocateError("发送/切换步骤仅接受一次点击，不允许复合动作")
                for action_index, action in enumerate(recipe["actions"]):
                    if action_index:
                        observation = self.observe()
                    if observation.package != package:
                        raise LocateError("修复动作前前台应用改变")
                    if action["type"] == "tap" or "target" in action:
                        point = locate(action["target"], observation, self.store.assets)
                    action_before = observation
                    self.state["inflight"] = {"step": step, "risk": risk, "before": time.time()}
                    self.save()
                    if before_action is not None and action_index == 0:
                        before_action()
                    action_dispatched = True
                    self._act(action, observation, package, exploration=source in {"agent", "saved", "candidate"} and risk != "send")
                    time.sleep(.35)
                deadline = time.monotonic() + 12
                while True:
                    observation = self.observe()
                    if observation.package == package and postcondition(observation):
                        break
                    if time.monotonic() >= deadline:
                        raise LocateError("动作后目标页面/状态未通过验证")
                    time.sleep(.25)
                deferred = source not in {"saved", "builtin"} and step.startswith("capture.")
                if deferred:
                    self.state.setdefault("flow_candidates", {})[step] = {
                        "recipe": recipe, "context": self._context(rule_context), "request": str(request_file) if request_file else None}
                    self.log("capture_method_candidate", step=step, reusable=False)
                elif source not in {"saved", "builtin"}:
                    self.store.promote(package, step, recipe, rule_context, source)
                    self.log("rule_promoted", step=step, source=source)
                elif learned:
                    self.store.record_success(package, step, learned["revision"], rule_context)
                if request_file:
                    request = read_json(request_file)
                    request.update(status="candidate" if deferred else "verified", finished_at=time.time())
                    write_json(request_file, request)
                self.state.pop("inflight", None)
                self.state.pop("pending_request", None)
                if source == "candidate" and not deferred:
                    self.state.get("flow_candidates", {}).pop(step, None)
                self.teach.discard(step)
                self.state["status"] = "running"
                self.save()
                self.log("step_verified", step=step, source=source)
                return observation
            except NeedsRepair:
                raise
            except (LocateError, RuntimeError, OSError) as error:
                if not action_dispatched or risk == "tap":
                    self.state.pop("inflight", None)
                self.log("step_failed", step=step, source=source, error=str(error))
                if source == "saved":
                    self.store.invalidate(package, step, str(error), learned["revision"], rule_context.version)
                if request_file:
                    request = read_json(request_file)
                    request.update(status="rejected", error=str(error))
                    write_json(request_file, request)
                self.state.pop("pending_request", None)
                self.save()
                observation = self.observe()
                if risk == "toggle" and action_before is not None:
                    w, h = action_before.physical_size
                    region = [0, h * .10, w, h * .88]
                    if (observation.package == action_before.package and observation.physical_size == action_before.physical_size
                            and stable(crop(action_before.png, region), crop(observation.png, region))):
                        # A full verification timeout with an unchanged panel proves this target was inert.
                        self.state.pop("inflight", None)
                        self.save()
                        self.log("toggle_no_effect_confirmed", step=step)
                if risk in {"send", "toggle"} and self.state.get("inflight", {}).get("step") == step:
                    self._request(step, goal, observation, hints, "动作可能已执行，仅允许只读确认，不重放", uncertain_send=risk == "send", uncertain_action=risk == "toggle")
                if source in {"saved", "candidate"} and observation.package == package and not self.state.get("inflight") and not self.state.get("exploration"):
                    recipe, source, learned, request_file = pick_candidate(observation)
                else:
                    recipe = None
        raise RuntimeError(f"步骤 {step} 的恢复次数已耗尽")
