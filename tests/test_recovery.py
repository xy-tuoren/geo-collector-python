"""Exercise persisted repair/resume boundaries without connecting to a device."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from geo_collector.device import Observation
from geo_collector import recovery
from geo_collector.recovery import NeedsRepair, StepEngine, submit_repair
from geo_collector.rules import FAILURE_LIMIT, RuleStore
from geo_collector.storage import read_json, write_json


PACKAGE = "com.example.collector"


def observation(*, color="white", ready=False):
    stream = BytesIO()
    Image.new("RGB", (180, 400), color).save(stream, format="PNG")
    nodes = [{"text": text, "bounds": box} for text, box in [
        ("打开", [20, 100, 70, 140]), ("备用入口", [90, 100, 160, 140]),
    ]]
    if ready:
        nodes.append({"text": "已打开", "bounds": [20, 200, 160, 240]})
    return Observation(stream.getvalue(), "<hierarchy/>", PACKAGE, "MainActivity", (180, 400), (180, 400), "1", nodes)


def recipe(label="打开"):
    return {"actions": [{"type": "tap", "target": {"kind": "node", "attr": "text", "value": label}}]}


def is_ready(current):
    return any(node.get("text") == "已打开" for node in current.nodes)


class FakeDevice:
    def __init__(self, before, after=None):
        self.current, self.after = before, after
        self.actions = []

    def observe(self, *, ocr):
        assert ocr is True
        return self.current

    def tap(self, point, *, expected_package):
        assert expected_package == self.current.package
        self.actions.append(tuple(point))
        if self.after is not None:
            self.current = self.after

    def swipe(self, start, end, *, expected_package):
        assert expected_package == self.current.package
        self.actions.append((tuple(start), tuple(end)))
        if self.after is not None:
            self.current = self.after


@pytest.fixture(autouse=True)
def no_real_wait(monkeypatch):
    ticks = iter(range(0, 10000, 20))
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: 1000.0, sleep=lambda _: None))


def engine(tmp_path, *, device=None, store=None, state=None):
    device = device or FakeDevice(observation())
    store = store or RuleStore(tmp_path / "rules")
    state = state if state is not None else {"entry": "native", "index": 1}
    return StepEngine(device, store, tmp_path / "run", state)


def perform(machine, **kwargs):
    return machine.perform("open", "打开目标页", PACKAGE, is_ready, **kwargs)


def request_for(machine, **kwargs):
    with pytest.raises(NeedsRepair) as caught:
        perform(machine, **kwargs)
    file = Path(caught.value.request)
    return file, read_json(file)


def test_only_verified_repair_is_promoted_then_reused_without_agent(tmp_path):
    machine = engine(tmp_path, device=FakeDevice(observation(), observation(ready=True)))
    file, request = request_for(machine)
    assert machine.store.get(PACKAGE, "open") is None
    submit_repair(file, plan=recipe(), sha256=request["sha256"])
    assert machine.store.get(PACKAGE, "open") is None
    assert not machine.device.actions

    assert is_ready(perform(machine))
    saved = machine.store.get(PACKAGE, "open")
    assert saved["source"] == "agent" and saved["recipe"] == recipe()
    assert read_json(file)["status"] == "verified"
    assert "pending_request" not in machine.state and "inflight" not in machine.state
    assert len(machine.device.actions) == 1

    next_question = engine(tmp_path / "next", device=FakeDevice(observation(), observation(ready=True)),
                           store=RuleStore(machine.store.root), state={"entry": "native", "index": 2})
    assert is_ready(perform(next_question))
    assert len(next_question.device.actions) == 1
    assert next_question.state.get("agent_requests", 0) == 0
    assert next_question.store.get(PACKAGE, "open")["revision"] == saved["revision"]


@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("change", ["video", "overlay", "activity", "label", "moved"])
def test_repair_on_animated_page_requires_unchanged_named_control(tmp_path, scale, change):
    before = observation()
    bounds = [20 * scale, 40 * scale, 70 * scale, 70 * scale]
    stream = BytesIO()
    Image.new("RGB", (180 * scale, 400 * scale), "white").save(stream, format="PNG")
    before = replace(before, png=stream.getvalue(), logical_size=(180 * scale, 400 * scale),
                     physical_size=(180 * scale, 400 * scale), nodes=[
                         {"text": "搜索", "resource_id": "app:id/search", "bounds": bounds, "enabled": True, "clickable": True}])
    stream = BytesIO()
    Image.new("RGB", before.physical_size, "black").save(stream, format="PNG")
    after = replace(before, png=stream.getvalue(), nodes=[dict(before.nodes[0])])
    if change == "video":
        after.nodes.insert(0, {"text": "", "bounds": [0, 0, 180 * scale, 400 * scale], "enabled": True, "clickable": False})
    elif change == "overlay":
        after.nodes.append({"text": "广告", "bounds": bounds, "enabled": True, "clickable": True})
    elif change == "activity":
        after = replace(after, activity="OtherActivity")
    elif change == "label":
        after.nodes[0]["text"] = "购买"
    elif change == "moved":
        after.nodes[0]["bounds"] = [n + 20 * scale for n in bounds]
    machine = engine(tmp_path, device=FakeDevice(before, observation(ready=True)))
    file, request = request_for(machine)
    plan = {"actions": [{"type": "tap", "target": {"kind": "node", "attr": "resource_id", "value": "app:id/search"}}]}
    submit_repair(file, plan=plan, sha256=request["sha256"])
    machine.device.current = after
    if change == "video":
        assert is_ready(perform(machine))
        assert len(machine.device.actions) == 1
        assert machine.store.get(PACKAGE, "open")["source"] == "agent"
    else:
        with pytest.raises(NeedsRepair):
            perform(machine)
        assert not machine.device.actions
        assert read_json(file)["status"] == "stale"


@pytest.mark.parametrize("matches", [0, 2])
def test_repair_rejects_invalid_original_selector_then_accepts_corrected_target(tmp_path, matches):
    before = observation()
    before.nodes = [
        {"text": "更多", "resource_id": "app:id/icon", "bounds": [10, 40, 40, 70], "clickable": True},
        {"text": "搜索", "resource_id": "app:id/icon", "bounds": [140, 40, 170, 70], "clickable": True},
    ]
    machine = engine(tmp_path, device=FakeDevice(before, observation(ready=True)))
    file, request = request_for(machine)
    target = {"kind": "node", "attr": "resource_id", "value": "app:id/icon" if matches else "missing"}
    plan = {"actions": [{"type": "tap", "target": target}], "explore": True}
    with pytest.raises(ValueError, match=f"原始现场.*实际匹配 {matches} 个"):
        submit_repair(file, plan=plan, sha256=request["sha256"])
    assert read_json(file)["status"] == "pending"
    assert not (file.parent / "response.json").exists() and not machine.device.actions

    target.update(value="app:id/icon", region=[.75, 0, 1, .25])
    submit_repair(file, plan=plan, sha256=request["sha256"])
    machine.device.current = replace(before, png=observation(color="black").png)
    assert is_ready(perform(machine))
    assert machine.device.actions == [(155.0, 55.0)]
    assert machine.state["agent_requests"] == 1
    assert machine.store.get(PACKAGE, "open") is None, "exploration still requires whole-question validation"


def test_animated_page_accepts_stable_whitelisted_ocr_control():
    before = observation()
    before.nodes = [{"text": "", "class_name": "android.view.ViewGroup", "bounds": [20, 40, 160, 100],
                     "enabled": True, "clickable": False}]
    before.texts = [{"text": "小荷AI医生", "confidence": .99, "bounds": [40, 50, 130, 80]}]
    stream = BytesIO()
    Image.new("RGB", before.physical_size, "black").save(stream, format="PNG")
    after = replace(before, png=stream.getvalue(), nodes=[dict(before.nodes[0])], texts=[dict(before.texts[0])])
    plan = {"actions": [{"type": "tap", "target": {"kind": "ocr", "pattern": "小荷AI医生"}}]}
    assert recovery.unchanged_control(before, after, plan, Path("/unused"))
    after.texts[0]["text"] = "其他AI医生"
    assert not recovery.unchanged_control(before, after, plan, Path("/unused"))


def test_failed_agent_action_is_not_promoted(tmp_path):
    machine = engine(tmp_path)
    first_file, first = request_for(machine)
    submit_repair(first_file, plan=recipe(), sha256=first["sha256"])
    next_file, next_request = request_for(machine)
    assert next_file != first_file and next_request["status"] == "pending"
    assert read_json(first_file)["status"] == "rejected"
    assert machine.store.get(PACKAGE, "open") is None
    assert len(machine.device.actions) == 1


def test_saved_rule_failure_requests_repair_and_replacement_is_verified(tmp_path):
    machine = engine(tmp_path)
    machine.store.promote(PACKAGE, "open", recipe(), machine.device.current, "builtin")
    file, request = request_for(machine)
    # One failure is counted, not fatal: the rule stays active with a failure mark.
    saved = machine.store.get(PACKAGE, "open")
    assert saved["active"] and saved["failures"] == 1
    assert len(machine.device.actions) == 1
    assert request["status"] == "pending"

    machine.device.after = observation(ready=True)
    replacement = recipe("备用入口")
    submit_repair(file, plan=replacement, sha256=request["sha256"])
    assert is_ready(perform(machine))
    saved = machine.store.get(PACKAGE, "open")
    assert saved["recipe"] == replacement and saved["revision"] == 2
    assert machine.device.actions == [(45.0, 120.0), (125.0, 120.0)]
    assert len(list((machine.store.root / "history").glob("*.json"))) == 1


def test_saved_selector_that_disappears_is_invalidated_without_clicking(tmp_path):
    machine = engine(tmp_path)
    machine.store.promote(PACKAGE, "open", recipe("旧版入口"), machine.device.current, "agent")
    for attempt in range(1, FAILURE_LIMIT + 1):
        # Each attempt is a separate question; a pending request never re-tries saved rules.
        machine = engine(tmp_path / f"q{attempt}", store=machine.store, state={"entry": "native", "index": attempt})
        _, request = request_for(machine)
        assert request["status"] == "pending"
        assert not machine.device.actions
        saved = machine.store.all()["rules"][f"{PACKAGE}:open"]
        assert saved["failures"] == attempt
        assert saved["active"] is (attempt < FAILURE_LIMIT)
    assert machine.store.get(PACKAGE, "open") is None
    # The same rule becomes eligible again on a different app version.
    assert machine.store.get(PACKAGE, "open", replace(machine.device.current, version="2")) is not None
    assert machine.store.get(PACKAGE, "open", machine.device.current) is None


def test_learned_interrupt_is_dismissed_before_the_step_and_counted_when_it_stops_working(tmp_path):
    blocked = observation()
    blocked.nodes += [{"text": "广告", "bounds": [10, 150, 170, 350], "clickable": True},
                      {"text": "关闭", "description": "关闭", "bounds": [150, 150, 170, 170], "clickable": True}]

    class Phone(FakeDevice):
        def tap(self, point, *, expected_package):
            self.actions.append(tuple(point))
            if self.current is blocked and point == (160.0, 160.0):
                self.current = observation()
            elif self.current is not blocked:
                self.current = observation(ready=True)

    interrupt = {"interrupt": {"detect": {"kind": "node", "attr": "text", "value": "广告"},
                               "actions": [{"type": "tap", "target": {"kind": "node", "attr": "text", "value": "关闭"}}]}}
    machine = engine(tmp_path, device=Phone(blocked))
    machine.store.promote(PACKAGE, "interrupt.ad", interrupt, blocked, "agent")
    machine.store.promote(PACKAGE, "open", recipe(), observation(), "agent")
    assert is_ready(perform(machine))
    assert machine.device.actions == [(160.0, 160.0), (45.0, 120.0)]
    assert machine.state.get("agent_requests", 0) == 0
    health = {row["step"]: row for row in machine.store.health()}
    assert health[f"{PACKAGE}:interrupt.ad"]["successes"] == 1

    class StuckPhone(FakeDevice):
        def tap(self, point, *, expected_package):
            self.actions.append(tuple(point))

    stuck = engine(tmp_path / "stuck", device=StuckPhone(blocked), store=machine.store, state={"entry": "native", "index": 2})
    with pytest.raises(NeedsRepair):
        perform(stuck)
    assert stuck.device.actions[0] == (160.0, 160.0)
    assert machine.store.all()["rules"][f"{PACKAGE}:interrupt.ad"]["failures"] == 1


def test_saved_rule_success_resets_failures_and_records_version(tmp_path):
    machine = engine(tmp_path, device=FakeDevice(observation(), observation(ready=True)))
    machine.store.promote(PACKAGE, "open", recipe(), machine.device.current, "agent")
    machine.store.invalidate(PACKAGE, "open", "瞬态失败", 1, "1")
    assert machine.store.get(PACKAGE, "open")["failures"] == 1
    machine.device.current = replace(machine.device.current, version="9")
    assert is_ready(perform(machine))
    saved = machine.store.get(PACKAGE, "open")
    assert saved["failures"] == 0 and saved["successes"] == 1
    assert saved["verified_versions"] == ["1", "9"]
    assert machine.state.get("agent_requests", 0) == 0


def test_response_requires_original_sha_is_write_once_and_rejects_closed_request(tmp_path):
    machine = engine(tmp_path)
    file, request = request_for(machine)
    with pytest.raises(ValueError, match="SHA256"):
        submit_repair(file, plan=recipe(), sha256="0" * 64)
    response_file = file.parent / "response.json"
    assert not response_file.exists()
    submit_repair(file, plan=recipe(), sha256=request["sha256"])
    first_response = response_file.read_bytes()
    with pytest.raises(FileExistsError):
        submit_repair(file, plan=recipe("备用入口"), sha256=request["sha256"])
    assert response_file.read_bytes() == first_response
    request["status"] = "stale"
    write_json(file, request)
    with pytest.raises(ValueError, match="已经结束"):
        submit_repair(file, plan=recipe(), sha256=request["sha256"])
    assert not machine.device.actions


@pytest.mark.parametrize("field,value", [("request_id", "another-request"), ("sha256", "f" * 64)])
def test_consumer_rejects_response_for_another_observation(tmp_path, field, value):
    machine = engine(tmp_path)
    file, request = request_for(machine)
    response = submit_repair(file, plan=recipe(), sha256=request["sha256"])
    response[field] = value
    write_json(file.parent / "response.json", response)
    with pytest.raises(ValueError, match="另一份现场"):
        perform(machine)
    assert not machine.device.actions
    assert machine.store.get(PACKAGE, "open") is None


def test_changed_current_page_expires_request_before_any_action(tmp_path):
    machine = engine(tmp_path)
    file, request = request_for(machine)
    submit_repair(file, plan=recipe(), sha256=request["sha256"])
    machine.device.current = observation(color="black")
    next_file, next_request = request_for(machine)
    assert next_file != file
    assert read_json(file)["status"] == "stale"
    assert next_request["sha256"] != request["sha256"]
    assert not machine.device.actions
    assert machine.store.get(PACKAGE, "open") is None


def test_postcondition_completed_while_waiting_closes_repair_without_replay(tmp_path, monkeypatch):
    before, completed = observation(), observation(color="black", ready=True)
    machine = engine(tmp_path, device=FakeDevice(before), state={
        "entry": "native", "index": 1,
        "inflight": {"step": "open", "risk": "toggle", "before": 900.0},
    })
    machine.wait = 180
    remaining = iter([before, completed])

    def observe(*, ocr):
        assert ocr is True
        machine.device.current = next(remaining)
        return machine.device.current

    monkeypatch.setattr(machine.device, "observe", observe)
    assert perform(machine, risk="toggle") is completed
    requests = list((machine.run_dir / "调试产物" / "repairs").glob("*/request.json"))
    assert len(requests) == 1
    assert read_json(requests[0])["status"] == "satisfied_without_replay"
    assert not (requests[0].parent / "response.json").exists()
    assert not machine.device.actions
    assert machine.store.get(PACKAGE, "open") is None
    saved = read_json(machine.run_dir / "调试产物" / "run.json")
    assert saved["status"] == "running" and saved["agent_requests"] == 1
    assert "pending_request" not in saved and "inflight" not in saved


def test_postcondition_completed_when_consuming_response_never_uses_stale_repair(tmp_path, monkeypatch):
    before, completed = observation(), observation(color="black", ready=True)
    machine = engine(tmp_path, device=FakeDevice(before))
    file, request = request_for(machine)
    submit_repair(file, plan=recipe(), sha256=request["sha256"])
    remaining = iter([before, completed])

    def observe(*, ocr):
        assert ocr is True
        machine.device.current = next(remaining)
        return machine.device.current

    monkeypatch.setattr(machine.device, "observe", observe)
    assert perform(machine) is completed
    assert read_json(file)["status"] == "satisfied_without_replay"
    assert list(file.parent.parent.glob("*/request.json")) == [file]
    assert not machine.device.actions
    assert machine.store.get(PACKAGE, "open") is None
    saved = read_json(machine.run_dir / "调试产物" / "run.json")
    assert saved["status"] == "running" and saved["agent_requests"] == 1
    assert "pending_request" not in saved and "inflight" not in saved


def test_modified_saved_screenshot_is_rejected_even_if_current_page_matches_it(tmp_path):
    machine = engine(tmp_path, device=FakeDevice(observation(), observation(ready=True)))
    file, request = request_for(machine)
    submit_repair(file, plan=recipe(), sha256=request["sha256"])
    modified = observation(color="black")
    (file.parent / "screen.png").write_bytes(modified.png)
    machine.device.current = modified
    with pytest.raises((ValueError, NeedsRepair)):
        perform(machine)
    assert not machine.device.actions
    assert machine.store.get(PACKAGE, "open") is None


@pytest.mark.parametrize("risk,uncertainty_field", [("send", "uncertain_send"), ("toggle", "uncertain_action")])
def test_uncertain_action_is_never_replayed_and_readonly_confirmation_clears_checkpoint(tmp_path, risk, uncertainty_field):
    # A changed but unrecognized toggle result may already be expanded;
    # repeating the action could close it again. A send remains uncertain
    # even when the image did not change.
    device = FakeDevice(observation(), observation(color="black") if risk == "toggle" else None)
    machine = engine(tmp_path, device=device)
    file, request = request_for(machine, defaults=[recipe()], risk=risk)
    assert request[uncertainty_field] is True
    assert len(machine.device.actions) == 1
    with pytest.raises(ValueError, match="结果不确定"):
        submit_repair(file, plan=recipe(), sha256=request["sha256"])
    second_file, _ = request_for(machine, defaults=[recipe()], risk=risk)
    assert second_file == file
    assert len(machine.device.actions) == 1

    # A resumed run may discover that the first send actually succeeded.
    machine.device.current = observation(ready=True)
    assert is_ready(perform(machine, defaults=[recipe()], risk=risk))
    assert len(machine.device.actions) == 1
    assert "inflight" not in machine.state and "pending_request" not in machine.state
    assert read_json(file)["status"] != "pending"


def test_inert_toggle_after_full_postcondition_timeout_can_use_a_new_repair_target(tmp_path):
    machine = engine(tmp_path)
    file, request = request_for(machine, defaults=[recipe()], risk="toggle")
    assert not request["uncertain_action"] and not request["uncertain_send"]
    assert "inflight" not in machine.state
    assert machine.device.actions == [(45.0, 120.0)]
    assert machine.store.get(PACKAGE, "open") is None

    # The first location had no effect. A new semantic target is allowed,
    # but the repair is still only persisted after the actual state opens.
    submit_repair(file, plan=recipe("备用入口"), sha256=request["sha256"])
    machine.device.after = observation(ready=True)
    assert is_ready(perform(machine, risk="toggle"))
    assert machine.device.actions == [(45.0, 120.0), (125.0, 120.0)]
    assert machine.store.get(PACKAGE, "open")["recipe"] == recipe("备用入口")
    assert read_json(file)["status"] == "verified"


def test_send_validator_rejection_before_dispatch_does_not_mark_send_uncertain(tmp_path):
    machine = engine(tmp_path)
    def reject_changed_input():
        raise RuntimeError("发送前输入框不再等于原题")
    _, request = request_for(machine, defaults=[recipe()], risk="send", before_action=reject_changed_input)
    assert not machine.device.actions
    assert request["uncertain_send"] is False
    assert "inflight" not in machine.state
    assert machine.store.get(PACKAGE, "open") is None


def test_wrong_foreground_stops_before_request_or_action(tmp_path):
    device = FakeDevice(replace(observation(), package="com.example.other"))
    machine = engine(tmp_path, device=device)
    with pytest.raises(RuntimeError, match="要求前台"):
        perform(machine, defaults=[recipe()])
    assert not device.actions
    assert machine.state.get("agent_requests", 0) == 0


@pytest.mark.parametrize("physical,logical", [((1080, 2400), (720, 1600)), ((720, 1600), (1080, 2400))])
def test_repaired_drawer_swipe_starts_on_ocr_header_and_maps_distance_to_logical_screen(tmp_path, physical, logical):
    pw, ph = physical
    lw, lh = logical
    stream = BytesIO()
    Image.new("RGB", physical, "white").save(stream, format="PNG")
    before = Observation(stream.getvalue(), "<hierarchy/>", PACKAGE, "MainActivity", logical, physical, "1",
                         nodes=[{"class_name": "RecyclerView", "bounds": [0, lh * .40, lw, lh * .98]}],
                         texts=[{"text": "参考药品", "confidence": .99, "bounds": [pw * .2, ph * .31, pw * .65, ph * .35]},
                                {"text": "参考药品", "confidence": .99, "bounds": [pw * .2, ph * .60, pw * .65, ph * .64]}])
    expanded = replace(before, nodes=[{"class_name": "RecyclerView", "bounds": [0, lh * .18, lw, lh * .98]}])
    machine = engine(tmp_path, device=FakeDevice(before, expanded))
    def expand():
        return machine.perform("products.expand", "从标题区域展开药品抽屉", PACKAGE,
                               lambda current: current.nodes[0]["bounds"][1] <= current.logical_size[1] * .20)

    with pytest.raises(NeedsRepair) as caught:
        expand()
    file = Path(caught.value.request)
    plan = {"actions": [{"type": "swipe", "direction": "down", "distance_fraction": .18,
                         "target": {"kind": "ocr", "pattern": "^参考药品$", "region": [0, .15, 1, .4]}}]}
    submit_repair(file, plan=plan, sha256=read_json(file)["sha256"])
    assert machine.store.get(PACKAGE, "products.expand") is None
    assert expand() is expanded
    assert len(machine.device.actions) == 1
    start, end = machine.device.actions[0]
    assert start == pytest.approx((lw * .425, lh * .33))
    assert end == pytest.approx((lw * .425, lh * .15))
    assert start[1] < before.nodes[0]["bounds"][1]  # Title, not product contents.
    assert machine.store.get(PACKAGE, "products.expand")["recipe"] == plan


def test_capture_review_describes_missing_results_without_allowing_unchecked_movement(tmp_path):
    machine = engine(tmp_path, state={"entry": "douyin", "index": 1, "phase": "capture", "mode": "current",
        "tasks": [{"entry": "douyin", "index": 1, "text": "完整原题"}], "capture_objective": "capture.body",
        "body_resume": {"frame_files": ["first.png", "second.png"]}})
    with pytest.raises(NeedsRepair) as stopped:
        machine._request("capture.review", "取得完整连续正文", observation(), (), "最后两帧接缝未通过")
    file = Path(stopped.value.request)
    request = read_json(file)
    assert request["objective"]["id"] == "capture.body"
    assert request["question"] == "完整原题" and request["missing_evidence"] == ["最后两帧接缝未通过"]
    assert request["checkpoint"]["body_frames"] == 2
    assert request["readonly"] and not request["uncertain_action"]
    assert "repair.explore" not in request["allowed_resolutions"]
    assert "recover.reenter-answer" in request["allowed_resolutions"]
    assert "recover.restart-app" not in request["allowed_resolutions"]
    for plan in (recipe(), {**recipe(), "explore": True}):
        with pytest.raises(ValueError, match="完整性复查"):
            submit_repair(file, plan=plan, sha256=request["sha256"])
    assert not machine.device.actions and not (file.parent / "response.json").exists()


def test_stale_saved_route_falls_back_to_builtin_default_without_promoting_builtin(tmp_path):
    from geo_collector.goals import page_guard

    healthy = observation()
    healthy.nodes.append({"package": PACKAGE, "resource_id": "app:id/header", "text": "搜索", "bounds": [10, 10, 80, 40]})
    stale_route = {"route": [{
        "action": recipe("重试")["actions"][0],
        "before": page_guard(healthy),
        "after": page_guard(healthy),
    }]}
    machine = engine(tmp_path, device=FakeDevice(healthy, observation(ready=True)))
    machine.store.promote(PACKAGE, "open", stale_route, healthy, "agent")

    result = perform(machine, defaults=[recipe("打开")])
    assert is_ready(result)
    assert machine.state.get("agent_requests", 0) == 0
    assert machine.device.actions == [(45.0, 120.0)]
    saved = machine.store.all()["rules"][f"{PACKAGE}:open"]
    assert saved["revision"] == 1 and saved["failures"] == 1 and saved["source"] == "agent"


def test_transient_exploration_and_route_pruning_exclude_systemui_and_retry_hops(tmp_path):
    from geo_collector.goals import page_guard, prune_route

    error_page = observation()
    error_page.nodes = [
        {"package": "com.android.systemui", "resource_id": "com.android.systemui:id/status_bar", "bounds": [0, 0, 180, 20]},
        {"package": PACKAGE, "resource_id": f"{PACKAGE}:id/2vk", "bounds": [0, 20, 180, 60]},
        {"package": PACKAGE, "resource_id": f"{PACKAGE}:id/search_bar", "text": "", "bounds": [10, 25, 140, 55]},
        {"package": PACKAGE, "text": "重试", "bounds": [60, 180, 120, 220], "clickable": True},
    ]
    loaded_page = replace(error_page, nodes=[
        error_page.nodes[0], error_page.nodes[2],
        {"package": PACKAGE, "text": "打开", "bounds": [20, 100, 70, 140], "clickable": True},
    ])
    guard = page_guard(error_page)
    assert guard["anchors"] == [{"kind": "node", "attr": "resource_id", "value": f"{PACKAGE}:id/search_bar"}]

    class RetryThenOpenPhone(FakeDevice):
        def tap(self, point, *, expected_package):
            self.actions.append(tuple(point))
            self.current = loaded_page if point == (90.0, 200.0) else observation(ready=True)

    machine = engine(tmp_path, device=RetryThenOpenPhone(error_page))
    file, req = request_for(machine)
    submit_repair(file, plan={**recipe("重试"), "transient": True}, sha256=req["sha256"])
    with pytest.raises(NeedsRepair) as second_stop:
        perform(machine)
    assert read_json(file)["status"] == "transient"
    assert machine.state.get("transient_recovery_steps") == 1
    assert "exploration" not in machine.state

    second_file = Path(second_stop.value.request)
    submit_repair(second_file, plan={**recipe("打开"), "explore": True}, sha256=read_json(second_file)["sha256"])
    assert is_ready(perform(machine))
    machine.commit_flows()
    promoted = machine.store.get(PACKAGE, "open")["recipe"]["route"]
    assert len(promoted) == 1 and promoted[0]["action"]["target"]["value"] == "打开"

    # Even if a retry hop were recorded without --transient, prune_route strips it.
    noisy = [
        {"action": recipe("重试")["actions"][0], "before": guard, "after": page_guard(loaded_page)},
        {"action": recipe("打开")["actions"][0], "before": page_guard(loaded_page), "after": page_guard(observation(ready=True))},
    ]
    assert len(prune_route(noisy)) == 1 and prune_route(noisy)[0]["action"]["target"]["value"] == "打开"


def test_commit_flows_drops_product_entry_when_drawer_was_not_used(tmp_path):
    machine = engine(tmp_path)
    scene = observation()
    machine.state["flow_candidates"] = {
        "perception.products_entry": {
            "recipe": {"perception": {"products_entry": {"kind": "ocr", "pattern": "^思密达·蒙脱石散$"}}},
            "context": machine._context(scene),
            "used": True,
        }
    }
    machine.commit_flows({"products": {"present": True, "mode": "inline"}})
    assert machine.store.get(PACKAGE, "perception.products_entry") is None
