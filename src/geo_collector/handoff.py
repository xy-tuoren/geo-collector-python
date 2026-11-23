"""Small, bounded descriptions of a paused collector; never a full run transcript."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .storage import read_json


def _text(value, limit=240):
    return str(value or "")[:limit]


def _inside(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("接管现场文件不在原批次目录内")
    return path


def _target(node, size):
    attr = next((key for key in ("resource_id", "description", "text") if node.get(key)), None)
    if attr is None:
        return None
    bounds = node.get("bounds", [])
    if len(bounds) != 4 or len(size) != 2 or not all(size):
        return None
    # A semantic anchor plus its observed region disambiguates repeated labels.
    return {"kind": "node", "attr": attr, "value": str(node[attr]),
            "region": [max(0, bounds[0] / size[0] - .005), max(0, bounds[1] / size[1] - .005),
                       min(1, bounds[2] / size[0] + .005), min(1, bounds[3] / size[1] + .005)]}


def _failure_anchors(observation):
    """Only semantic controls count as a changed failure, not a moving clock.

    Raw pixels, generated paragraphs, video captions, timestamps and invocation
    IDs are deliberately absent. A changed checkpoint or navigation control is
    evidence to ask again; a different animation frame by itself is not.
    """
    from .rules import UI_LABELS

    size = observation.get("logical_size", [0, 0])
    if len(size) != 2 or not all(size):
        return []
    anchors = []
    for node in observation.get("nodes", []):
        bounds = node.get("bounds", [])
        if (node.get("package", "") not in ("", observation.get("package")) or not node.get("enabled", True)
                or len(bounds) != 4 or bounds[3] <= size[1] * .045 or bounds[1] >= size[1] * .97):
            continue
        label = node.get("text") or node.get("description") or ""
        ui_label = label if label in UI_LABELS else None
        interactive = node.get("clickable") or node.get("scrollable") or node.get("class_name", "").endswith("EditText")
        if not interactive and not ui_label:
            continue
        # Keep unknown short button names only when they lack a stable ID.
        anchor = node.get("resource_id") or ui_label or (label if len(label) <= 32 else None)
        if not anchor:
            continue
        position = [round(bounds[i] / size[i % 2], 2) for i in range(4)]
        item = {"anchor": anchor, "label": ui_label, "bounds": position,
                "clickable": bool(node.get("clickable")), "scrollable": bool(node.get("scrollable"))}
        if item not in anchors:
            anchors.append(item)
    physical = observation.get("physical_size", [0, 0])
    if len(physical) == 2 and all(physical):
        for line in observation.get("texts", []):
            bounds = line.get("bounds", [])
            if (line.get("confidence", 0) < .78 or line.get("text") not in UI_LABELS or len(bounds) != 4
                    or bounds[3] <= physical[1] * .045 or bounds[1] >= physical[1] * .97):
                continue
            item = {"ocr": line["text"], "bounds": [round(bounds[i] / physical[i % 2], 2) for i in range(4)]}
            if item not in anchors:
                anchors.append(item)
    return sorted(anchors, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))


def _relevance(label, request):
    label = re.sub(r"\s+", "", str(label))
    if not label:
        return 0
    hints = [re.sub(r"\s+", "", str(hint)) for hint in request.get("hints", []) if hint]
    if label in hints:
        return 1000
    if any(hint in label or label in hint for hint in hints):
        return 700
    objective = request.get("objective") or {}
    goal = str(request.get("goal", "")) + str(objective.get("description", ""))
    return 400 if 2 <= len(label) <= 24 and label in goal else 0


def _node_rank(node, size, request):
    from .rules import UI_LABELS
    label = node.get("text") or node.get("description") or ""
    bounds = node["bounds"]
    area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) / (size[0] * size[1])
    semantic = _relevance(label, request)
    score = semantic + (80 if node.get("class_name", "").endswith("Button") else 0)
    score += 60 if label in UI_LABELS else 0
    score += 30 if node.get("resource_id") and label else 0
    score += 20 if node.get("clickable") else 0
    score += 10 if label else -200
    score += 8 if bounds[1] / size[1] < .2 or bounds[3] / size[1] > .85 else 0
    score -= 250 if area > .3 else 0
    return (-score, area)


def _rank_nodes(nodes, size, request):
    ranked, identities = [], set()
    for node in sorted(nodes, key=lambda item: _node_rank(item, size, request)):
        label = (node.get("text") or node.get("description") or "").strip()
        identity = (tuple(node["bounds"]), label)
        if identity in identities:
            continue
        identities.add(identity)
        # Named nested parent/child controls refer to the same visible action.
        # Prefer the higher-ranked stable anchor instead of consuming two slots.
        duplicate = False
        for other in ranked:
            if not label or label != (other.get("text") or other.get("description") or "").strip():
                continue
            a, b = node["bounds"], other["bounds"]
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
            if intersection >= smaller * .9:
                duplicate = True
                break
        if not duplicate:
            ranked.append(node)
    return ranked


def scene_candidates(request, observation, limit=24):
    """Stable IDs within one saved scene, shared by the host CLI and observer."""
    from .goals import BEFORE_SEND, CAPTURE_OBJECTIVES, SESSION_HINTS, SESSION_STEP
    if request.get("step") in {SESSION_STEP, BEFORE_SEND}:
        request = {**request, "hints": [*request.get("hints", []), *SESSION_HINTS],
                   "objective": {"description": CAPTURE_OBJECTIVES[request["step"]][0]}}
    size = observation.get("logical_size", [0, 0])
    nodes = [node for node in observation.get("nodes", [])
             if node.get("package", "") in ("", observation.get("package"))
             and node.get("enabled", True) and _target(node, size)]
    # Rank the current goal's anchors before social feed buttons and containers.
    # Never truncate a raw hierarchy before this pass.
    nodes = _rank_nodes(nodes, size, request)
    targets, candidates = {}, []
    for index, node in enumerate(nodes[:limit]):
        key = f"n{index + 1}"
        targets[key] = _target(node, size)
        candidate = {"id": key, "at": [round((node["bounds"][0] + node["bounds"][2]) / size[0] * 50),
                                          round((node["bounds"][1] + node["bounds"][3]) / size[1] * 50)]}
        label = node.get("text") or node.get("description")
        if label:
            candidate["label"] = _text(label, 80)
        if node.get("resource_id"):
            candidate["anchor"] = _text(node["resource_id"].rsplit("/", 1)[-1], 60)
        for flag in ("clickable", "scrollable"):
            if node.get(flag):
                candidate[flag] = True
        if node.get("class_name", "").endswith("EditText"):
            candidate["input"] = True
        candidates.append(candidate)
    ocr = [line for line in observation.get("texts", []) if line.get("confidence", 0) >= .78]
    physical = observation.get("physical_size", [0, 0])
    ocr.sort(key=lambda line: -_relevance(line.get("text", ""), request))
    for index, line in enumerate(ocr[:12]):
        key = f"o{index + 1}"
        bounds = line.get("bounds", [])
        if len(bounds) != 4 or len(physical) != 2 or not all(physical):
            continue
        targets[key] = {"kind": "ocr", "pattern": "^" + re.escape(line["text"]) + "$",
                        "region": [max(0, bounds[0] / physical[0] - .005), max(0, bounds[1] / physical[1] - .005),
                                   min(1, bounds[2] / physical[0] + .005), min(1, bounds[3] / physical[1] + .005)]}
        candidates.append({"id": key, "label": _text(line["text"], 100),
                           "at": [round((bounds[0] + bounds[2]) / physical[0] * 50),
                                  round((bounds[1] + bounds[3]) / physical[1] * 50)], "ocr": True})
    return targets, candidates, len(nodes) > limit or len(ocr) > 12


def build_handoff(run_directory, status=None, *, limit=24):
    """Read saved evidence only. ``status`` may supply a fresh teach-status scene.

    ``prompt`` is the only part sent to a model. Targets and paths remain local.
    The original PNG is attached only on a model's explicit need_image response.
    """
    if not 1 <= limit <= 48:
        raise ValueError("候选控件上限应为 1～48")
    root = Path(run_directory).resolve()
    state = read_json(root / "调试产物" / "run.json")
    pending = state.get("pending_request")
    request = read_json(_inside(pending, root)) if pending else {}
    status = status or {}
    task = status.get("task") or next((item for item in state.get("tasks", [])
                                     if (item.get("entry"), item.get("index")) ==
                                     (state.get("entry"), state.get("index"))), None)
    task = task or next((item for item in state.get("tasks", []) if item.get("status") != "complete"), {})
    from .goals import BEFORE_SEND, SESSION_STEP, repair_context
    if request.get("step") in {SESSION_STEP, BEFORE_SEND}:
        # Enrich outstanding requests created by an older CLI without rewriting the checkpoint.
        details = repair_context(state, request["step"], request.get("reason", ""))
        request = {**request, **{key: details[key] for key in ("objective", "missing_evidence", "checkpoint")}}
    scene = status.get("scene") or request.get("observation")
    if scene is None and pending:
        scene = str(Path(pending).parent)
    if scene:
        scene = _inside(scene, root)
        observation_file = _inside(scene if scene.name == "observation.json" else scene / "observation.json", root)
        observation = read_json(observation_file)
        screenshot = _inside(observation_file.parent / "screen.png", root)
        digest = hashlib.sha256(screenshot.read_bytes()).hexdigest()
    else:
        observation, screenshot, digest = {}, None, None
    targets, candidates, truncated = scene_candidates(request, observation, limit)
    objective = request.get("objective") or {}
    prompt = {"task": {**{key: task.get(key) for key in ("entry", "index", "phase")},
                       "text": _text(task.get("text"), 500), "text_truncated": len(task.get("text", "")) > 500},
              "step": request.get("step") or state.get("exploration", {}).get("step"),
              "objective": {"description": _text(objective.get("description") or request.get("goal")),
                            "required_evidence": [_text(item, 120) for item in objective.get("required_evidence", [])[:8]]},
              "missing_evidence": [_text(item) for item in request.get("missing_evidence", [request.get("reason")])[:4]],
              "allowed_resolutions": request.get("allowed_resolutions", ["resume"]),
              "checkpoint": {key: request.get("checkpoint", {}).get(key)
                             for key in ("preparation", "body_frames", "products_phase", "session_stage",
                                         "confirmation_dispatched", "question_dispatch_pending")
                             if key in request.get("checkpoint", {}) or key in ("preparation", "body_frames", "products_phase")},
              "page": {key: observation.get(key) for key in ("package", "activity", "logical_size", "physical_size")},
              "candidates": candidates, "candidates_truncated": truncated,
              "goal_check": status.get("goal_check"),
              "readonly": bool(request.get("readonly")),
              "uncertain": bool(state.get("inflight") or state.get("exploration", {}).get("dispatched")
                                or request.get("uncertain_send") or request.get("uncertain_action"))}
    # Do not send history, filesystem paths, device identity, raw XML, or question lists.
    fingerprint_data = {key: prompt[key] for key in ("task", "step", "objective", "checkpoint", "page")}
    fingerprint_data["anchors"] = _failure_anchors(observation)
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"prompt": prompt, "targets": targets, "fingerprint": fingerprint,
            "request": str(Path(pending).resolve()) if pending else None,
            "request_sha256": request.get("sha256"), "scene": str(scene) if scene else None,
            "screenshot": str(screenshot) if screenshot else None, "sha256": digest,
            "task_key": f"{task.get('entry')}:{task.get('index')}", "run_directory": str(root)}


def handoff_summary(run_directory, *, limit=24):
    """CLI-friendly offline summary with local artifact links, no device access."""
    root = Path(run_directory).resolve()
    state = read_json(root / "调试产物" / "run.json")
    if not state.get("pending_request") and not state.get("exploration"):
        return {"status": state.get("status", "unknown"), "pending": False, "run_directory": str(root),
                "request": None, "screenshot": None, "scene": None}
    context = build_handoff(run_directory, limit=limit)
    return {"status": state.get("status", "needs_repair"), "pending": True, **context["prompt"],
            "fingerprint": context["fingerprint"], "request": context["request"], "sha256": context["sha256"],
            "preview": "repair <request> --sha256 <sha256> --preview [--candidate <id> --role <role> | --plan <file>]",
            "screenshot": context["screenshot"], "scene": context["scene"]}
