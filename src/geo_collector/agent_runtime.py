"""Bind bounded AI suggestions to the collector's existing verified actions."""
from pathlib import Path
import hashlib
import re

from .images import stable
from .recovery import load_observation, unchanged_control
from .rules import in_region
from .teach import TeachSession
from .supervisor import CodexAgent, decision_plan, supervise


def _same_target(original, current, target):
    """Screen stability cannot override a changed semantic target anywhere."""
    kind = target.get("kind")
    if kind not in {"node", "ocr"}:
        return False  # Handoffs expose only observed nodes and OCR anchors.
    region = target.get("region", [0, 0, 1, 1])
    attrs = ("package", "class_name", "resource_id", "text", "description",
             "enabled", "clickable", "scrollable", "focused") if kind == "node" else ("text",)
    size = original.logical_size if kind == "node" else original.physical_size

    def selected(observation):
        candidates = observation.nodes if kind == "node" else observation.texts
        matches = []
        for item in candidates:
            if not in_region(item["bounds"], size, region):
                continue
            if kind == "node":
                if (item.get(target["attr"]) != target["value"]
                        or item.get("package") not in (None, "", observation.package)):
                    continue
            elif item.get("confidence", 0) < .78 or not re.search(target["pattern"], item["text"]):
                continue
            matches.append(item)
        return sorted(matches, key=lambda item: (tuple(item["bounds"]), tuple(str(item.get(key)) for key in attrs)))

    before, after = selected(original), selected(current)
    return bool(before) and len(before) == len(after) and all(
        all(a.get(key) == b.get(key) for key in attrs)
        and all(abs(x - y) <= size[i % 2] * .01 for i, (x, y) in enumerate(zip(a["bounds"], b["bounds"])))
        for a, b in zip(before, after))


def apply_decision(session, decision, context):
    """A model cannot dispatch directly, change the task, or act on an old page."""
    plan = decision_plan(decision, context)
    if plan is None:
        raise ValueError("该决定不包含可执行动作")
    task = session.task or {}
    if context["task_key"] != f"{task.get('entry')}:{task.get('index')}":
        raise ValueError("接管建议不属于当前题目")
    pending, request = session.request()
    if (str(pending.resolve()) if pending else None) != context.get("request"):
        raise ValueError("接管请求已经变化，重新观察后再决定")
    scene = Path(context["scene"])
    original = load_observation(scene.parent if scene.name == "observation.json" else scene)
    if hashlib.sha256(original.png).hexdigest() != context["sha256"]:
        raise ValueError("接管原始截图已变化")

    if 'perception' in plan and request:
        from .repair_preview import OfflineRejection, preview_plan
        preview = preview_plan(request, original, plan)
        if preview['scene_check'] == 'rejected' or (preview['scene_check'] == 'failed' and {'session_welcome', 'session_divider'}.intersection(plan['perception'])):
            raise OfflineRejection('、'.join(preview['missing_evidence'])[:240])

    def validate(current):
        if (original.package != current.package or original.activity != current.activity
                or original.logical_size != current.logical_size or original.physical_size != current.physical_size):
            raise ValueError("AI 思考期间页面或设备尺寸已变化，未执行动作")
        check_plan = plan
        if "perception" in plan:
            target = next(iter(plan["perception"].values()))
            check_plan = {"actions": [{"type": "tap", "target": target}]}
        target = check_plan["actions"][0].get("target")
        if target is not None and not _same_target(original, current, target):
            raise ValueError("AI 思考期间目标控件已变化，未执行动作")
        if not (stable(original.png, current.png)
                or unchanged_control(original, current, check_plan, session.store.assets)):
            raise ValueError("AI 思考期间目标控件或页面已变化，未执行动作")

    # Validate the exact observation that TeachSession will use, avoiding a
    # second observation between freshness checks and dispatch/role binding.
    if "perception" in plan:
        role, target = next(iter(plan["perception"].items()))
        return session.perception(role, target, validate_observation=validate)
    return session.act(plan["actions"][0], validate_observation=validate)


def supervise_collection(device, store, directory, state, *, resume, limits, agent=None, retry_failed=False):
    """The caller retains its original device lease throughout the batch."""
    def inspect():
        return TeachSession(device, store, directory, state).status()

    def apply(decision, context):
        return apply_decision(TeachSession(device, store, directory, state), decision, context)

    return supervise(directory, resume=resume, inspect=inspect, apply=apply,
                     agent=agent or CodexAgent(timeout=limits.timeout), limits=limits, retry_failed=retry_failed)
