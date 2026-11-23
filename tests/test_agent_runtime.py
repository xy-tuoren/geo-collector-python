"""Offline boundaries between a model suggestion, teaching, and the CLI lease."""
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from geo_collector import agent_runtime, cli, device, workflow
from geo_collector.device import Observation
from geo_collector.platforms import PACKAGES
from geo_collector.recovery import NeedsRepair, save_observation
from geo_collector.rules import RuleStore
from geo_collector.storage import event, read_json, write_json
from geo_collector.supervisor import AgentReply
from geo_collector.teach import TeachSession


PACKAGE = PACKAGES["xiaohe"]


def scene(*, color="white", label="全部药品"):
    stream = BytesIO()
    Image.new("RGB", (180, 400), color).save(stream, format="PNG")
    node = {"resource_id": PACKAGE + ":id/action", "text": label,
            "description": "", "class_name": "android.widget.Button",
            "bounds": [20, 100, 80, 140], "package": PACKAGE,
            "enabled": True, "clickable": True}
    return Observation(stream.getvalue(), "<hierarchy/>", PACKAGE, "SameActivity",
                       (180, 400), (180, 400), "1", [node])


def decision(kind="tap", **overrides):
    return {"decision": kind, "candidate": "n1" if kind in {"tap", "perception"} else None,
            "role": "products_entry" if kind == "perception" else None,
            "direction": None, "distance": None, "reason": "依据本次现场选择控件", **overrides}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "run"
    original = scene()
    evidence = root / "调试产物" / "repairs" / "one"
    save_observation(evidence, original)
    pending = evidence / "request.json"
    write_json(pending, {"status": "pending", "step": "overlay.dismiss", "package": PACKAGE,
                         "allowed_resolutions": ["resume", "teach.explore", "repair.perception"]})
    state = {"entry": "xiaohe", "index": 1, "phase": "open", "status": "needs_repair",
             "pending_request": str(pending),
             "tasks": [{"entry": "xiaohe", "index": 1, "text": "完整原题", "phase": "open",
                        "status": "needs_repair"}]}

    class Phone:
        def __init__(self):
            self.current = original
            self.observations = []
            self.actions = []

        def observe(self, **_):
            if self.observations:
                self.current = self.observations.pop(0)
            return self.current

        def tap(self, point, *, expected_package):
            assert expected_package == self.current.package
            self.actions.append({"point": point, "page": self.current})

    phone = Phone()
    session = TeachSession(phone, RuleStore(tmp_path / "rules"), root, state)
    monkeypatch.setattr(session.engine, "observe", phone.observe)
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)
    context = {"run_directory": str(root.resolve()), "task_key": "xiaohe:1",
               "request": str(pending.resolve()), "scene": str(evidence / "observation.json"),
               "sha256": hashlib.sha256(original.png).hexdigest(),
               "targets": {"n1": {"kind": "node", "attr": "resource_id", "value": PACKAGE + ":id/action"}},
               "prompt": {"uncertain": False, "readonly": False,
                          "allowed_resolutions": ["resume", "teach.explore", "repair.perception"],
                          "candidates": [{"id": "n1", "label": "全部药品", "input": False}]}}
    return SimpleNamespace(session=session, context=context, phone=phone, original=original, request=pending)


def test_runtime_dispatches_one_navigation_action_against_original_scene(runtime):
    result = agent_runtime.apply_decision(runtime.session, decision(), runtime.context)
    assert result["recorded"] is True
    assert len(runtime.phone.actions) == 1
    assert runtime.phone.actions[0]["point"] == (50.0, 120.0)
    assert read_json(runtime.request)["status"] == "explored"


@pytest.mark.parametrize("field,value", [("task_key", "xiaohe:2"), ("request", "/different/request.json"),
                                         ("sha256", "0" * 64)])
def test_runtime_rejects_changed_task_request_or_saved_evidence(runtime, field, value):
    runtime.context[field] = value
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision(), runtime.context)
    assert runtime.phone.actions == []


@pytest.mark.parametrize("changes", [
    {"activity": "OtherActivity"}, {"logical_size": (360, 800)}, {"physical_size": (360, 800)},
])
def test_runtime_rejects_page_or_geometry_change_before_navigation(runtime, changes):
    runtime.phone.current = replace(runtime.original, **changes)
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision(), runtime.context)
    assert runtime.phone.actions == []


def test_runtime_rejects_changed_page_and_repurposed_control(runtime):
    runtime.phone.current = scene(color="black", label="继续")
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision(), runtime.context)
    assert runtime.phone.actions == []


@pytest.mark.parametrize("kind", ["tap", "perception"])
def test_runtime_allows_video_pixels_to_change_when_semantic_control_is_unchanged(runtime, kind):
    runtime.phone.current = scene(color="black")
    result = agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    if kind == "tap":
        assert result["recorded"] and len(runtime.phone.actions) == 1
    else:
        assert result["bound"] and not runtime.phone.actions


@pytest.mark.parametrize("box", [[20, 20, 80, 35], [20, 365, 80, 385]], ids=["top", "bottom"])
@pytest.mark.parametrize("kind", ["tap", "perception"])
@pytest.mark.parametrize("same_pixels", [False, True], ids=["changed-pixels", "same-pixels"])
def test_runtime_target_changes_cannot_hide_outside_middle_crop(runtime, box, kind, same_pixels):
    original = replace(runtime.original, nodes=[{**runtime.original.nodes[0], "bounds": box}])
    save_observation(Path(runtime.context["scene"]).parent, original)
    image = Image.open(BytesIO(original.png)).convert("RGB")
    if not same_pixels:
        ImageDraw.Draw(image).rectangle(box, fill="black")
    stream = BytesIO()
    image.save(stream, format="PNG")
    runtime.phone.current = replace(original, png=stream.getvalue(), nodes=[{**original.nodes[0], "text": "继续"}])
    with pytest.raises(ValueError, match="目标控件"):
        agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    assert not runtime.phone.actions and not runtime.session.state.get("flow_candidates")


@pytest.mark.parametrize("kind", ["tap", "perception"])
@pytest.mark.parametrize("change", [{"description": "新的目标"}, {"enabled": False},
                                    {"bounds": [90, 100, 150, 140]}])
def test_runtime_same_png_cannot_override_target_identity_or_position(runtime, kind, change):
    runtime.phone.current = replace(runtime.original, nodes=[{**runtime.original.nodes[0], **change}])
    with pytest.raises(ValueError, match="目标控件"):
        agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    assert not runtime.phone.actions and not runtime.session.state.get("flow_candidates")


def test_runtime_unchanged_large_perception_target_remains_supported(runtime):
    original = replace(runtime.original, nodes=[{**runtime.original.nodes[0], "bounds": [10, 50, 170, 330]}])
    save_observation(Path(runtime.context["scene"]).parent, original)
    runtime.phone.current = original
    result = agent_runtime.apply_decision(runtime.session, decision("perception"), runtime.context)
    assert result["bound"] and not runtime.phone.actions


@pytest.mark.parametrize("kind", ["tap", "perception"])
def test_runtime_rejects_changed_ocr_candidate_even_with_identical_png(runtime, kind):
    original = replace(runtime.original, texts=[{"text": "全部药品", "confidence": .99, "bounds": [20, 100, 80, 140]}])
    save_observation(Path(runtime.context["scene"]).parent, original)
    runtime.context["targets"]["n1"] = {"kind": "ocr", "pattern": "全部药品|继续"}
    runtime.phone.current = replace(original, texts=[{**original.texts[0], "text": "继续"}])
    with pytest.raises(ValueError, match="目标控件"):
        agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    assert not runtime.phone.actions and not runtime.session.state.get("flow_candidates")


def test_runtime_perception_must_reject_stale_same_activity_target(runtime):
    # A shared resource ID on a different in-app page is not the observed role.
    runtime.phone.current = scene(color="black", label="继续")
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision("perception"), runtime.context)
    assert not runtime.session.state.get("flow_candidates")


def test_runtime_never_dispatches_against_an_unvalidated_second_observation(runtime):
    changed = scene(color="black", label="继续")
    runtime.phone.observations = [runtime.original, changed, changed]
    try:
        agent_runtime.apply_decision(runtime.session, decision(), runtime.context)
    except ValueError:
        pass
    # Either reuse the validated observation or validate the final observation;
    # never select a newly repurposed control using the old model suggestion.
    assert all(item["page"].nodes[0]["text"] == "全部药品" for item in runtime.phone.actions)


@pytest.mark.parametrize("kind", ["tap", "perception"])
def test_runtime_uncertain_context_never_applies_a_model_suggestion(runtime, kind):
    runtime.context["prompt"]["uncertain"] = True
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    assert not runtime.phone.actions and not runtime.session.state.get("flow_candidates")


@pytest.mark.parametrize("key", ["uncertain_send", "uncertain_action"])
@pytest.mark.parametrize("kind", ["tap", "perception"])
def test_runtime_rechecks_uncertainty_saved_after_model_handoff(runtime, key, kind):
    request = read_json(runtime.request)
    request[key] = True
    write_json(runtime.request, request)
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision(kind), runtime.context)
    assert not runtime.phone.actions and not runtime.session.state.get("flow_candidates")


@pytest.mark.parametrize("uncertainty", ["inflight", "dispatched"])
def test_runtime_perception_rechecks_uncertain_state_after_handoff(runtime, uncertainty):
    if uncertainty == "inflight":
        runtime.session.state["inflight"] = {"step": "xiaohe.send"}
    else:
        runtime.session.state["exploration"] = {"step": "overlay.dismiss", "dispatched": {"action": "tap"}}
    with pytest.raises(ValueError):
        agent_runtime.apply_decision(runtime.session, decision("perception"), runtime.context)
    assert not runtime.session.state.get("flow_candidates")


@pytest.fixture
def cli_fakes(monkeypatch):
    state = SimpleNamespace(leased=False, closed=False, serials=[], trace=[])

    class Lease:
        def __init__(self, *args):
            pass

        def __enter__(self):
            assert not state.leased
            state.leased = True
            state.trace.append("leased")

        def __exit__(self, *_):
            assert state.closed
            state.leased = False
            state.trace.append("released")

    class Phone:
        def __init__(self, serial=None, **_):
            assert state.leased
            state.serials.append(serial)
            self.serial = serial or "offline-phone"

        def close(self):
            assert state.leased
            state.closed = True
            state.trace.append("closed")

    monkeypatch.setattr(cli, "DeviceLease", Lease)
    monkeypatch.setattr(device, "Device", Phone)
    return state


def test_cli_holds_phone_lease_through_inference_and_emits_single_json(tmp_path, monkeypatch, capsys, cli_fakes):
    def collect(phone, store, root, state, **_):
        assert cli_fakes.leased and not cli_fakes.closed
        cli_fakes.trace.append("collected")
        event(root / "调试产物" / "events.jsonl", "local_capture", frame=1)

    def supervise(phone, store, root, state, *, resume, **_):
        assert cli_fakes.leased and state["serial"] == phone.serial
        cli_fakes.trace.append("inference")
        return resume()

    monkeypatch.setattr(workflow, "run_questions", collect)
    monkeypatch.setattr(cli, "verify_run", lambda _: {"complete": True, "tasks": 1})
    monkeypatch.setattr(agent_runtime, "supervise_collection", supervise)
    code = cli.main(["run", "--entry", "xiaohe", "--question", "完整原题", "--agent", "codex", "--events",
                     "--output", str(tmp_path / "out"), "--rule-dir", str(tmp_path / "rules")])
    output = capsys.readouterr()
    assert code == 0 and json.loads(output.out) == {"complete": True, "tasks": 1}
    assert len(output.out.strip().splitlines()) == 1
    assert '"event": "run_opened"' in output.err and '"event": "local_capture"' in output.err
    assert cli_fakes.trace == ["leased", "inference", "collected", "closed", "released"]


def test_completed_cli_batch_makes_zero_model_and_inspection_calls(tmp_path, monkeypatch, capsys, cli_fakes):
    def collect(phone, store, root, state, **_):
        state["status"] = "complete"
        write_json(root / "调试产物" / "run.json", state)

    class UncalledAgent:
        def __init__(self, **_):
            pass

        def __call__(self, *_args, **_kwargs):
            pytest.fail("A completed batch must never invoke a model")

    monkeypatch.setattr(workflow, "run_questions", collect)
    monkeypatch.setattr(cli, "verify_run", lambda _: {"complete": True, "tasks": 1})
    monkeypatch.setattr(agent_runtime, "CodexAgent", UncalledAgent)
    monkeypatch.setattr(TeachSession, "status", lambda *a, **k: pytest.fail("No inspection is needed after success"))
    code = cli.main(["run", "--question", "完整原题", "--agent", "codex", "--entry", "xiaohe",
                     "--output", str(tmp_path / "out"), "--rule-dir", str(tmp_path / "rules")])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["complete"] and result["supervisor"]["calls"] == 0
    assert cli_fakes.closed and not cli_fakes.leased


def test_real_supervisor_keeps_original_lease_during_model_callback(tmp_path, monkeypatch, capsys, cli_fakes):
    calls = []

    def paused(phone, store, root, state, **_):
        evidence = root / "调试产物" / "repairs" / "one"
        save_observation(evidence, scene())
        request = evidence / "request.json"
        write_json(request, {"status": "pending", "step": "overlay.dismiss", "package": PACKAGE,
                             "goal": "关闭弹层", "allowed_resolutions": ["resume", "teach.explore"],
                             "screenshot": str(evidence / "screen.png"),
                             "observation": str(evidence / "observation.json")})
        state.update(entry="xiaohe", index=1, pending_request=str(request), status="needs_repair")
        write_json(root / "调试产物" / "run.json", state)
        raise NeedsRepair(request)

    class StopAgent:
        def __init__(self, **_):
            pass

        def __call__(self, context, *, include_image):
            assert cli_fakes.leased and not cli_fakes.closed
            assert read_json(Path(context["run_directory"]) / "调试产物" / "run.json")["serial"] == "offline-phone"
            calls.append((context["task_key"], include_image))
            return AgentReply(decision("stop"))

    # Exercise real supervisor dispatch while the observer and phone stay offline.
    monkeypatch.setattr(workflow, "run_questions", paused)
    monkeypatch.setattr(agent_runtime, "CodexAgent", StopAgent)
    monkeypatch.setattr(TeachSession, "status", lambda self: {"scene": str(self.request()[0].parent)})
    code = cli.main(["run", "--question", "完整原题", "--agent", "codex", "--entry", "xiaohe",
                     "--output", str(tmp_path / "out"), "--rule-dir", str(tmp_path / "rules")])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == 20 and result["supervisor"]["calls"] == 1
    assert len(output.out.strip().splitlines()) == 1
    assert calls == [("xiaohe:1", False)]
    assert cli_fakes.closed and not cli_fakes.leased


def test_cli_restores_original_phone_and_closes_it_on_handoff(tmp_path, monkeypatch, capsys, cli_fakes):
    root = tmp_path / "batch"
    write_json(root / "调试产物" / "run.json", {"serial": "saved-phone", "rule_root": str(tmp_path / "rules")})

    def paused(*args, **kwargs):
        assert cli_fakes.leased
        raise NeedsRepair(root / "调试产物" / "request.json")

    monkeypatch.setattr(workflow, "run_questions", paused)
    assert cli.main(["resume", str(root)]) == 20
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "needs_repair"
    assert cli_fakes.serials == ["saved-phone"] and cli_fakes.closed and not cli_fakes.leased


def test_cli_rejects_other_phone_before_acquiring_or_constructing_device(tmp_path, capsys, cli_fakes):
    root = tmp_path / "batch"
    write_json(root / "调试产物" / "run.json", {"serial": "saved-phone", "rule_root": str(tmp_path / "rules")})
    assert cli.main(["supervise", str(root), "--serial", "other-phone"]) == 1
    assert cli_fakes.trace == [] and cli_fakes.serials == []
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "failed" and "另一台手机" in result["error"]
    assert not output.err
