"""Agent-driven teaching: one observed device action per CLI call, no request ping-pong.

`run`/`resume` still stop with exit code 20 and a request.json when no rule fits.
Instead of answering that request with a single-action plan and resuming for
every step, the Agent drives the phone with `geo-collect teach <run> tap|swipe|back`
while the CLI records each hop with page evidence into the same `exploration`
checkpoint that `resume` already understands. When the goal is reached, `resume`
verifies it, keeps the route as a candidate and commits it once the question
passes acceptance. Sends, purchases and deletions stay forbidden here.
"""
from __future__ import annotations

from pathlib import Path
import re
import time

from .goals import ROLES, bind_roles, page_guard
from .platforms import PACKAGES, PLATFORMS
from .platforms.common import edit
from .recovery import StepEngine, save_observation
from .rules import LocateError, locate, present, validate_recipe
from .storage import read_json


READONLY_STEPS = {"capture.review", "products.images_wait", "douyin.results_ready", "toutiao.results_ready"}
NODE_ATTRS = {"text", "description", "resource_id", "class_name"}


def parse_target(node=None, ocr=None, region=None, offset=None):
    if bool(node) == bool(ocr):
        raise ValueError("定位需要 --node attr=value 或 --ocr pattern 之一")
    if node:
        attr, separator, value = node.partition("=")
        if not separator or attr not in NODE_ATTRS or not value:
            raise ValueError("--node 格式为 attr=value，attr 取 text/description/resource_id/class_name")
        target = {"kind": "node", "attr": attr, "value": value}
    else:
        target = {"kind": "ocr", "pattern": ocr}
    if region:
        target["region"] = [float(v) for v in region]
    if offset:
        target["offset"] = [float(v) for v in offset]
    validate_recipe({"actions": [{"type": "tap", "target": target}]})
    return target


def summarize(observation, limit=60):
    """Compact page inventory for the Agent; the PNG/XML remain the full record."""
    rows = []
    for node in observation.nodes:
        label = node.get("text") or node.get("description") or ""
        if not (label or node.get("resource_id")):
            continue
        if not (node.get("clickable") or node.get("scrollable") or node.get("focused")
                or node.get("class_name", "").endswith(("EditText", "Button"))):
            continue
        rows.append({"text": node.get("text", "")[:60], "description": node.get("description", "")[:60],
                     "resource_id": node.get("resource_id", ""), "class": node.get("class_name", "").rsplit(".", 1)[-1],
                     "bounds": list(node["bounds"]), "clickable": bool(node.get("clickable")), "focused": bool(node.get("focused"))})
    texts = [{"text": line["text"][:80], "bounds": list(line["bounds"])} for line in observation.texts if line.get("confidence", 0) >= .78]
    return {"package": observation.package, "activity": observation.activity, "version": observation.version,
            "logical_size": list(observation.logical_size), "physical_size": list(observation.physical_size),
            "controls": rows[:limit], "controls_total": len(rows), "ocr": texts[:limit], "ocr_total": len(texts)}


def goal_check(step, task):
    """Best-effort postcondition for a teaching status; `resume` is the real acceptance."""
    from . import capture
    entry = task["entry"] if task else None
    platform = PLATFORMS.get(entry)
    question = task["text"] if task else ""
    if platform is not None and callable(check_goal := getattr(platform, "goal_check", None)):
        check = check_goal(step, task)
        if check is not None:
            return check

    def viewport_of(observation):
        try:
            return capture.answer_viewport(observation)
        except capture.CaptureError:
            return None

    checks = {
        "xiaohe.new_session": lambda o: PLATFORMS["xiaohe"].clean_session(o),
        "products.close": lambda o: capture.drawer_state(o) is None,
        "capture.answer_page": capture.answer_page_ready,
        "capture.close_existing_products": lambda o: capture.drawer_state(o) is None and viewport_of(o) is not None,
        "network.retry": lambda o: not capture.network_error_visible(o),
        "overlay.dismiss": lambda o: not capture.guide_visible(o) and viewport_of(o) is not None,
        "capture.question_top": lambda o: (lambda v: bool(v and (b := capture.question_bubble(o, question, v))
                                                          and (b["bounds"][1] - v[1]) / (v[3] - v[1]) <= .20))(viewport_of(o)),
        "citation.expand": lambda o: (lambda v: bool(v and capture.citation_state(o, v)["expanded"]))(viewport_of(o)),
        "products.open": lambda o: capture.drawer_state(o) is not None,
        "products.expand": lambda o: bool((capture.drawer_state(o) or {}).get("expanded")),
    }
    if step and step.endswith(".focus_input") and entry:
        return lambda o: edit(o, entry, focused=True) is not None
    if platform is not None and platform.REENTER_SUPPORTED:
        if step == entry + ".open_search":
            return lambda o: edit(o, entry) is not None
        if step == entry + ".close_miniapp":
            return lambda o: platform.search_results_ready(o, question)
    if step in checks:
        return checks[step]
    if platform is not None and step == platform.ENTRY_STEP:
        return platform.answer_ready
    return None


class TeachSession:
    def __init__(self, device, store, run_dir, state):
        self.device, self.store = device, store
        self.run_dir, self.state = Path(run_dir).resolve(), state
        self.engine = StepEngine(device, store, self.run_dir, state, teach=state.get("teach", []))
        self.task = next((item for item in state.get("tasks", [])
                          if (item.get("entry"), item.get("index")) == (state.get("entry"), state.get("index"))), None)
        if self.task is None:
            self.task = next((item for item in state.get("tasks", []) if item.get("status") != "complete"), None)
        if self.task is not None:
            state.setdefault("entry", self.task["entry"])
            state.setdefault("index", self.task["index"])
        self.package = PACKAGES.get(self.task["entry"]) if self.task else None

    # -- request / checkpoint helpers -------------------------------------------------
    def request(self):
        pending = self.state.get("pending_request")
        if not pending or not Path(pending).exists():
            return None, None
        return Path(pending), read_json(Path(pending))

    def _scene(self, label, observation):
        directory = self.run_dir / "调试产物" / "teach"
        directory.mkdir(parents=True, exist_ok=True)
        ordinal = len([p for p in directory.iterdir() if p.is_dir()]) + 1
        target = directory / f"{ordinal:03d}-{label}"
        save_observation(target, observation)
        return str(target)

    def _observe(self):
        observation = self.engine.observe()
        if self.package and observation.package != self.package:
            raise RuntimeError(f"前台应用是 {observation.package}，本题需要 {self.package}；可用 `teach launch` 拉回前台")
        return observation

    def _step(self):
        _, request = self.request()
        if request:
            return request["step"]
        trace = self.state.get("exploration")
        if trace:
            return trace["step"]
        return None

    def _guard_actions(self, request, *, require_goal=True):
        if (self.state.get("inflight") or self.state.get("exploration", {}).get("dispatched")
                or (request and (request.get("uncertain_send") or request.get("uncertain_action")))):
            raise ValueError("上一个动作结果不确定：先 resume 做只读确认，不能继续操作")
        if request is None:
            if require_goal and not self.state.get("exploration"):
                raise ValueError("没有待修复请求或进行中的探索；先运行 run/resume 直到退出码 20，或使用 teach interrupt/perception")
            return
        step = request["step"]
        if request.get("readonly") or step in READONLY_STEPS or step.startswith("layout."):
            raise ValueError("该请求是只读完整性复查：只能 teach perception、resume 或 recover，不能移动页面")
        if step.endswith(".send") or step == "page.perception":
            raise ValueError("发送步骤和页面识别请求不接受探索动作")

    def status(self, *, observe=True):
        file, request = self.request()
        trace = self.state.get("exploration") or {}
        result = {"run_directory": str(self.run_dir), "status": self.state.get("status"),
                  "task": {k: self.task.get(k) for k in ("entry", "index", "text", "phase", "status")} if self.task else None,
                  "request": {k: request.get(k) for k in ("request_id", "step", "goal", "reason", "objective", "missing_evidence",
                                                          "allowed_resolutions", "checkpoint", "readonly", "uncertain_send",
                                                          "uncertain_action", "screenshot", "observation")} if request else None,
                  "exploration": {"step": trace.get("step"), "steps": len(trace.get("route", [])),
                                  "dispatched": bool(trace.get("dispatched"))} if trace else None}
        if observe:
            observation = self.engine.observe()
            result["scene"] = self._scene("status", observation)
            result["page"] = summarize(observation)
            step = self._step()
            check = goal_check(step, self.task) if step else None
            if self.package and observation.package != self.package:
                result["foreground_mismatch"] = f"前台应用是 {observation.package}，本题需要 {self.package}；可用 `teach launch` 拉回前台"
                result["goal_check"] = False
            elif check is None:
                result["goal_check"] = "unavailable" if step else None
            else:
                try:
                    result["goal_check"] = bool(check(observation))
                except Exception as error:  # noqa: BLE001 - diagnostics only
                    result["goal_check"] = f"error: {error}"
        result["next"] = ("goal reached: run `geo-collect resume` to verify and record the route"
                          if result.get("goal_check") is True else
                          "inspect scene PNG/XML, then `teach tap|swipe|back` one observed action, or `teach perception`")
        return result

    # -- exploration actions ----------------------------------------------------------
    def act(self, action, *, validate_observation=None):
        file, request = self.request()
        self._guard_actions(request)
        validate_recipe({"actions": [action], "explore": True})
        step = request["step"] if request else self.state["exploration"]["step"]
        package = request["package"] if request else self.state["exploration"]["context"]["package"]
        observation = self._observe()
        if validate_observation is not None:
            validate_observation(observation)
        if observation.package != package:
            raise RuntimeError("前台应用与请求不一致")
        trace = self.state.setdefault("exploration", {"step": step, "context": self.engine._context(observation), "route": []})
        if trace["step"] != step:
            raise ValueError(f"进行中的探索属于 {trace['step']}，与当前请求 {step} 不一致")
        if len(trace["route"]) >= 32:
            raise ValueError("单目标探索已达32步，保留证据，不能无限点击")
        if "target" in action:
            locate(action["target"], observation, self.store.assets)
        hop = {"action": action, "before": page_guard(observation)}

        def dispatching():
            trace["dispatched"] = hop
            self.engine.save()

        try:
            self.engine._act(action, observation, package, exploration=True, before_dispatch=dispatching)
        except Exception as error:
            if trace.get("dispatched"):
                # Dispatch may have succeeded; only resume can confirm the goal.
                try:
                    scene = self._scene("failed", self.device.observe(ocr=False))
                except Exception:  # noqa: BLE001 - diagnostics only
                    scene = None
                self.engine.log("teach_action_failed", step=step, error=str(error), scene=scene)
            raise
        time.sleep(.35)
        after = self.engine.observe()
        trace.pop("dispatched", None)
        result = {"action": action, "before": self._scene("before", observation)}
        if after.package != package:
            self.engine.save()
            self.engine.log("teach_left_app", step=step, package=after.package)
            result.update(after=self._scene("left-app", after), left_app=after.package, recorded=False,
                          next="use `teach launch` to bring the app back; this hop was not recorded")
            return result
        hop["after"] = page_guard(after)
        trace["route"].append(hop)
        self.state["goal_exploration_steps"] = self.state.get("goal_exploration_steps", 0) + 1
        if file and request.get("status") == "pending" and request["step"] == step:
            self.engine._close_request(file, "explored")
        else:
            self.state["status"] = "running"
            self.engine.save()
        check = goal_check(step, self.task)
        satisfied = None
        if check is not None:
            try:
                satisfied = bool(check(after))
            except Exception:  # noqa: BLE001
                satisfied = None
        self.engine.log("teach_action", step=step, ordinal=len(trace["route"]), goal_satisfied=satisfied)
        result.update(after=self._scene("after", after), recorded=True, route_steps=len(trace["route"]),
                      page=summarize(after), goal_check=satisfied if check is not None else "unavailable",
                      next=("goal reached: run `geo-collect resume` to verify and record the route" if satisfied
                            else "inspect the new scene and submit the next single action, or `resume` to let the run re-check"))
        return result

    def launch(self):
        if self.package is None:
            raise ValueError("没有当前题目，无法确定目标应用")
        current = self.device.foreground()[0]
        if current == self.package:
            return {"launched": False, "package": self.package}
        self._guard_actions(self.request()[1], require_goal=False)
        if self.state.get("mode") == "current" or self.state.get("products_resume"):
            raise ValueError("当前回答模式和药品终止阶段不允许切换应用")
        self.device.launch(self.package, restart=False)
        self.engine.log("teach_launch", package=self.package, restart=False)
        return {"launched": True, "package": self.package, "page": summarize(self.engine.observe())}

    # -- perception and interrupts ----------------------------------------------------
    def perception(self, role, target, *, validate_observation=None):
        if role not in ROLES:
            raise ValueError(f"未知页面角色 {role}；可选：{sorted(ROLES)}")
        plan = {"perception": {role: target}}
        file, request = self.request()
        if self.state.get("inflight") or self.state.get("exploration", {}).get("dispatched"):
            raise ValueError("操作结果不确定，必须先只读确认")
        if request and (request.get("uncertain_send") or request.get("uncertain_action")):
            raise ValueError("操作结果不确定，必须先只读确认")
        if request and request["step"].endswith(".results_ready"):
            raise ValueError("首屏加载检查只允许 resume 只读确认，不能修改页面角色")
        observation = self._observe()
        if validate_observation is not None:
            validate_observation(observation)
        bind_roles(plan, observation, require=True)
        key = "perception." + role
        self.state.setdefault("flow_candidates", {})[key] = {
            "recipe": plan, "context": self.engine._context(observation), "request": str(file) if file else None}
        if file and request.get("status") == "pending" and (request["step"] in {"page.perception", *READONLY_STEPS}
                                                             or request["step"].startswith("perception.")):
            self.engine._close_request(file, "candidate")
        self.engine.teach.discard("page.perception")
        if self.task:
            self.engine.teach.discard("page.perception." + self.task["entry"])
        self.engine.save()
        self.engine.log("page_roles_learned", roles=[role], reusable=False)
        return {"role": role, "target": target, "bound": True, "scene": self._scene("perception", observation),
                "next": "run `geo-collect resume`; the role is committed only after the whole question passes"}

    def interrupt(self, name, detect, actions, package=None):
        self._guard_actions(self.request()[1], require_goal=False)
        if not re.fullmatch(r"[a-z0-9_]{2,40}", name):
            raise ValueError("中断名称仅允许小写字母、数字和下划线")
        package = package or self.package
        if package is None:
            raise ValueError("请通过 --entry 指定目标应用")
        recipe = validate_recipe({"interrupt": {"detect": detect, "actions": actions}})
        observation = self.engine.observe()
        if observation.package != package:
            raise RuntimeError(f"前台应用是 {observation.package}，中断规则属于 {package}")
        if not present(detect, observation):
            raise LocateError("当前页面看不到该中断的检测目标，不能凭空保存")
        before = observation
        for index, action in enumerate(actions):
            if index:
                observation = self.engine.observe()
                if observation.package != package:
                    raise RuntimeError("关闭中断期间前台应用改变")
            self.engine._act(action, observation, package, exploration=True)
            time.sleep(.35)
        after = self.engine.observe()
        if after.package != package or present(detect, after):
            raise LocateError("动作后中断目标仍然可见，未保存规则")
        step = "interrupt." + name
        self.store.promote(package, step, recipe, before, "agent")
        self.engine.log("interrupt_rule_promoted", step=step)
        self.engine.save()
        return {"step": step, "promoted": True, "before": self._scene("interrupt-before", before),
                "after": self._scene("interrupt-after", after)}


def list_cases(run_dir):
    """Everything an Agent needs to pick up a stopped run, in one listing."""
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / "调试产物" / "run.json")
    pending = state.get("pending_request")
    requests = []
    for file in sorted((run_dir / "调试产物" / "repairs").glob("*/request.json")):
        request = read_json(file)
        requests.append({"request": str(file), "step": request.get("step"), "status": request.get("status"),
                         "entry": request.get("entry"), "question_index": request.get("question_index"),
                         "reason": request.get("reason"), "objective": (request.get("objective") or {}).get("id"),
                         "allowed_resolutions": request.get("allowed_resolutions"), "pending": str(file) == pending})
    failures = []
    for file in sorted((run_dir / "调试产物").glob("*/*/failure.json")):
        failure = read_json(file)
        failures.append({"failure": str(file), "entry": failure.get("entry"), "index": failure.get("index"),
                         "phase": failure.get("phase"), "type": failure.get("type"), "error": failure.get("error")})
    tasks = [{k: task.get(k) for k in ("entry", "index", "status", "phase", "outcome")} for task in state.get("tasks", [])]
    trace = state.get("exploration") or {}
    return {"run_directory": str(run_dir), "status": state.get("status"), "tasks": tasks,
            "pending_request": pending, "exploration": {"step": trace.get("step"), "steps": len(trace.get("route", []))} if trace else None,
            "requests": requests, "failures": failures,
            "agent_requests": state.get("agent_requests", 0), "recovery_actions": len(state.get("recovery_actions", []))}


def teach(device, store, run_dir, args):
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / "调试产物" / "run.json")
    if state.get("serial") and state["serial"] != device.serial:
        raise ValueError("不能在另一台手机上教学当前批次")
    session = TeachSession(device, store, run_dir, state)
    action = args.teach_action
    if action == "status" or action == "observe":
        return session.status()
    if action == "launch":
        return session.launch()
    if action == "tap":
        return session.act({"type": "tap", "target": parse_target(args.node, args.ocr, args.region, args.offset)})
    if action == "back":
        return session.act({"type": "back"})
    if action == "swipe":
        step = {"type": "swipe", "direction": args.direction}
        if args.distance is not None:
            step["distance_fraction"] = args.distance
        if args.node or args.ocr:
            step["target"] = parse_target(args.node, args.ocr, args.region, args.offset)
        return session.act(step)
    if action == "perception":
        return session.perception(args.role, parse_target(args.node, args.ocr, args.region, None))
    if action == "interrupt":
        detect = parse_target(args.detect_node, args.detect_ocr, None, None)
        dismiss = [{"type": "back"}] if args.back else [{"type": "tap", "target": parse_target(args.node, args.ocr, args.region, args.offset)}]
        package = PACKAGES[args.entry] if getattr(args, "entry", None) else None
        return session.interrupt(args.name, detect, dismiss, package)
    raise ValueError(f"未知教学动作 {action}")
