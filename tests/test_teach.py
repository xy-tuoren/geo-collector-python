"""Agent teaching drives the phone one observed action per call; resume verifies and records."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from geo_collector import cli
from geo_collector.goals import page_guard
from geo_collector.recovery import NeedsRepair, StepEngine
from geo_collector.rules import RuleStore
from geo_collector.storage import read_json, write_json
from geo_collector.teach import TeachSession, goal_check, list_cases, parse_target
from test_recovery import FakeDevice, PACKAGE, is_ready, observation


class Phone(FakeDevice):
    def __init__(self):
        super().__init__(observation())
        self.stage = 0

    def tap(self, point, **kwargs):
        self.actions.append(tuple(point))
        self.stage += 1
        if self.stage == 1:
            self.current = replace(observation(color="gray"), activity="Second", nodes=[
                {"text": "继续", "resource_id": "next", "bounds": [20, 50, 90, 90], "clickable": True}])
        else:
            self.current = observation(ready=True)


def run_state(index=1, **extra):
    return {"entry": "native", "index": index, "phase": "open", "status": "running",
            "tasks": [{"entry": "native", "index": index, "text": "完整原题", "phase": "open", "status": "running"}], **extra}


def stop(engine):
    with pytest.raises(NeedsRepair) as stopped:
        engine.perform("goal.open", "进入目标页", PACKAGE, is_ready)
    return Path(stopped.value.request)


@pytest.mark.parametrize("entry", ["douyin", "toutiao"])
def test_search_teaching_goals_use_the_selected_platform(monkeypatch, entry):
    from geo_collector.platforms import PLATFORMS

    page = object()
    monkeypatch.setattr("geo_collector.teach.edit", lambda current, selected: {} if current is page and selected == entry else None)
    monkeypatch.setattr(PLATFORMS[entry], "search_results_ready", lambda current, question: current is page and question == "原题")
    monkeypatch.setattr(PLATFORMS[entry], "answer_ready", lambda current: current is page)
    for suffix in ("open_search", "close_miniapp", "open_xiaohe"):
        check = goal_check(entry + "." + suffix, {"entry": entry, "text": "原题"})
        assert check(page) and not check(object())


def test_teach_records_each_observed_hop_and_resume_verifies_without_a_response_file(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)
    store, phone, run_dir = RuleStore(tmp_path / "rules"), Phone(), tmp_path / "run"
    request_file = stop(StepEngine(phone, store, run_dir, run_state()))
    assert read_json(request_file)["status"] == "pending"

    session = TeachSession(phone, store, run_dir, read_json(run_dir / "调试产物/run.json"))
    first = session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}})
    assert first["recorded"] and first["route_steps"] == 1
    assert read_json(request_file)["status"] == "explored"
    assert "pending_request" not in session.state and not (request_file.parent / "response.json").exists()

    # A second process continues the same exploration from the checkpoint.
    session = TeachSession(phone, store, run_dir, read_json(run_dir / "调试产物/run.json"))
    with pytest.raises(RuntimeError, match="匹配 0"):
        session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "不存在"}})
    second = session.act({"type": "tap", "target": {"kind": "node", "attr": "resource_id", "value": "next"}})
    assert second["route_steps"] == 2 and phone.actions == [(45.0, 120.0), (55.0, 70.0)]
    saved = read_json(run_dir / "调试产物/run.json")
    assert saved["exploration"]["step"] == "goal.open" and len(saved["exploration"]["route"]) == 2
    assert all(set(hop) == {"action", "before", "after"} for hop in saved["exploration"]["route"])
    assert saved["goal_exploration_steps"] == 2

    resumed = StepEngine(phone, store, run_dir, saved)
    assert is_ready(resumed.perform("goal.open", "进入目标页", PACKAGE, is_ready))
    assert len(phone.actions) == 2, "resume must not replay taught hops"
    assert "exploration" not in resumed.state
    assert len(resumed.state["flow_candidates"]["goal.open"]["recipe"]["route"]) == 2
    assert store.get(PACKAGE, "goal.open") is None, "a reached subgoal is not yet a published route"
    resumed.commit_flows()
    assert len(store.get(PACKAGE, "goal.open")["recipe"]["route"]) == 2

    replay = StepEngine(Phone(), store, tmp_path / "replay", run_state(2))
    assert is_ready(replay.perform("goal.open", "进入目标页", PACKAGE, is_ready))
    assert len(replay.device.actions) == 2 and replay.state.get("agent_requests", 0) == 0


def test_replayed_route_waits_for_delayed_final_postcondition(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    before, transition, ready = observation(), observation(color="gray"), observation(color="gray", ready=True)

    class DelayedPhone(FakeDevice):
        def __init__(self):
            super().__init__(before)
            self.after_tap = 0

        def tap(self, point, **kwargs):
            self.actions.append(tuple(point))
            self.after_tap = 1

        def observe(self, *, ocr):
            if self.after_tap:
                self.current = transition if self.after_tap == 1 else ready
                self.after_tap += 1
            return self.current

    store = RuleStore(tmp_path / "rules")
    store.promote(PACKAGE, "goal.open", {"route": [{
        "action": {"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}},
        "before": page_guard(before),
        "after": page_guard(transition),
    }]}, before, "agent")
    phone = DelayedPhone()
    result = StepEngine(phone, store, tmp_path / "run", run_state()).perform(
        "goal.open", "进入目标页", PACKAGE, is_ready)
    assert is_ready(result) and len(phone.actions) == 1


def test_teach_refuses_actions_on_readonly_or_uncertain_requests_and_send_labels(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    store, run_dir = RuleStore(tmp_path / "rules"), tmp_path / "run"
    phone = FakeDevice(observation())
    machine = StepEngine(phone, store, run_dir, run_state(phase="capture", mode="current", capture_objective="capture.body"))
    with pytest.raises(NeedsRepair):
        machine._request("capture.review", "取得完整连续正文", observation(), (), "接缝未通过")
    session = TeachSession(phone, store, run_dir, read_json(run_dir / "调试产物/run.json"))
    with pytest.raises(ValueError, match="只读"):
        session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}})
    assert not phone.actions
    status = session.status()
    assert status["request"]["step"] == "capture.review" and status["request"]["readonly"]
    assert Path(status["scene"], "screen.png").exists()

    sending = FakeDevice(observation())
    sending.current.nodes[0]["text"] = "发送"
    machine = StepEngine(sending, store, tmp_path / "send", run_state())
    stop(machine)
    session = TeachSession(sending, store, tmp_path / "send", read_json(tmp_path / "send/调试产物/run.json"))
    with pytest.raises(RuntimeError, match="发送"):
        session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "发送"}})
    assert not sending.actions and "dispatched" not in session.state.get("exploration", {})

    uncertain = StepEngine(FakeDevice(observation()), store, tmp_path / "uncertain", run_state())
    with pytest.raises(NeedsRepair):
        uncertain._request("native.send", "确认已发送", observation(), (), "结果不确定", uncertain_send=True)
    session = TeachSession(uncertain.device, store, tmp_path / "uncertain", read_json(tmp_path / "uncertain/调试产物/run.json"))
    with pytest.raises(ValueError, match="不确定"):
        session.act({"type": "back"})


def test_teach_action_failure_after_dispatch_requires_readonly_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)

    class Flaky(FakeDevice):
        def tap(self, point, **kwargs):
            self.actions.append(tuple(point))
            raise OSError("simulated connection loss")

    store, phone, run_dir = RuleStore(tmp_path / "rules"), Flaky(observation()), tmp_path / "run"
    stop(StepEngine(phone, store, run_dir, run_state()))
    session = TeachSession(phone, store, run_dir, read_json(run_dir / "调试产物/run.json"))
    with pytest.raises(OSError):
        session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}})
    saved = read_json(run_dir / "调试产物/run.json")
    assert saved["exploration"]["dispatched"] and saved["exploration"]["route"] == []
    session = TeachSession(phone, store, run_dir, saved)
    with pytest.raises(ValueError, match="不确定"):
        session.act({"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}})
    assert len(phone.actions) == 1
    phone.current = observation(ready=True)
    resumed = StepEngine(phone, store, run_dir, saved)
    assert is_ready(resumed.perform("goal.open", "进入目标页", PACKAGE, is_ready))
    assert len(phone.actions) == 1 and "exploration" not in saved
    assert store.get(PACKAGE, "goal.open") is None


@pytest.mark.parametrize("operation", ["act", "interrupt", "launch"])
@pytest.mark.parametrize("checkpoint", ["inflight", "dispatched", "uncertain_send", "uncertain_action",
                                        "capture.review", "layout.answer_container", "readonly", "toutiao.results_ready"])
def test_all_teaching_navigation_preserves_blocked_checkpoints(tmp_path, checkpoint, operation):
    class NavigationPhone(FakeDevice):
        def foreground(self):
            return "com.example.other", "Other"

        def launch(self, package, *, restart):
            self.actions.append(("launch", package, restart))

    phone = NavigationPhone(observation(), observation(ready=True))
    store, run_dir, state = RuleStore(tmp_path / "rules"), tmp_path / "run", run_state()
    if checkpoint == "inflight":
        state["inflight"] = {"step": "goal.open", "risk": "toggle"}
    elif checkpoint == "dispatched":
        # A completed teaching hop already removed pending_request before this crash.
        state["exploration"] = {"step": "goal.open", "route": [], "dispatched": {"action": {"type": "back"}}}
    else:
        request = {"step": checkpoint if "." in checkpoint else "goal.open", "package": PACKAGE, "status": "pending"}
        if checkpoint in {"uncertain_send", "uncertain_action", "readonly"}:
            request[checkpoint] = True
        file = tmp_path / "request.json"
        write_json(file, request)
        state["pending_request"] = str(file)
    session = TeachSession(phone, store, run_dir, state)
    session.package = PACKAGE
    action = {"type": "tap", "target": parse_target(node="text=打开")}
    with pytest.raises(ValueError, match="不确定|只读"):
        if operation == "act":
            session.act(action)
        elif operation == "interrupt":
            session.interrupt("dialog", action["target"], [action])
        else:
            session.launch()
    assert not phone.actions and not store.file.exists()


def test_teach_perception_and_interrupt_are_bound_on_the_live_page(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)
    store, run_dir = RuleStore(tmp_path / "rules"), tmp_path / "run"
    page = observation()
    page.nodes.append({"text": "这是完整原题", "resource_id": "app:id/question", "package": PACKAGE, "bounds": [20, 300, 160, 340]})

    class Dialog(FakeDevice):
        def tap(self, point, **kwargs):
            self.actions.append(tuple(point))
            self.current = page

    blocked = replace(page, nodes=[*page.nodes, {"text": "稍后再说", "bounds": [40, 360, 140, 390], "clickable": True},
                                   {"text": "发现新版本", "bounds": [40, 200, 140, 230]}])
    phone = Dialog(blocked)
    session = TeachSession(phone, store, run_dir, run_state())
    with pytest.raises(ValueError, match="小写"):
        session.interrupt("Bad Name", parse_target(node="text=发现新版本"), [{"type": "tap", "target": parse_target(node="text=稍后再说")}], PACKAGE)
    result = session.interrupt("update_dialog", parse_target(node="text=发现新版本"),
                               [{"type": "tap", "target": parse_target(node="text=稍后再说")}], PACKAGE)
    assert result["promoted"] and phone.actions == [(90.0, 375.0)]
    rule = store.get(PACKAGE, "interrupt.update_dialog")
    assert rule["source"] == "agent" and rule["recipe"]["interrupt"]["detect"]["value"] == "发现新版本"
    with pytest.raises(RuntimeError, match="看不到"):
        session.interrupt("update_dialog", parse_target(node="text=发现新版本"), [{"type": "back"}], PACKAGE)

    bound = session.perception("question", parse_target(node="resource_id=app:id/question"))
    assert bound["bound"]
    candidate = session.state["flow_candidates"]["perception.question"]
    assert candidate["recipe"] == {"perception": {"question": {"kind": "node", "attr": "resource_id", "value": "app:id/question"}}}
    assert store.get(PACKAGE, "perception.question") is None
    with pytest.raises(RuntimeError, match="未找到"):
        session.perception("citations_header", parse_target(node="resource_id=missing"))
    with pytest.raises(ValueError, match="未知页面角色"):
        session.perception("bogus", parse_target(node="resource_id=app:id/question"))


def test_parse_target_validates_shape():
    assert parse_target(node="resource_id=app:id/x", region=[0, 0, 1, .5]) == {
        "kind": "node", "attr": "resource_id", "value": "app:id/x", "region": [0, 0, 1, .5]}
    assert parse_target(ocr="^搜索$", offset=[.5, .5]) == {"kind": "ocr", "pattern": "^搜索$", "offset": [.5, .5]}
    for bad in ({"node": "bogus=1"}, {"node": "text="}, {}, {"node": "text=a", "ocr": "b"}):
        with pytest.raises(ValueError):
            parse_target(**bad)


def test_cases_lists_requests_failures_and_exploration(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)
    store, phone, run_dir = RuleStore(tmp_path / "rules"), Phone(), tmp_path / "run"
    request_file = stop(StepEngine(phone, store, run_dir, run_state()))
    listing = list_cases(run_dir)
    assert listing["pending_request"] == str(request_file)
    assert listing["requests"][0]["step"] == "goal.open" and listing["requests"][0]["pending"]
    assert listing["tasks"] == [{"entry": "native", "index": 1, "status": "running", "phase": "open", "outcome": None}]
    TeachSession(phone, store, run_dir, read_json(run_dir / "调试产物/run.json")).act(
        {"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}})
    listing = list_cases(run_dir)
    assert listing["pending_request"] is None and listing["exploration"] == {"step": "goal.open", "steps": 1}
    assert listing["requests"][0]["status"] == "explored"


def test_cli_parses_teach_and_cases_commands():
    args = cli.parser().parse_args(["teach", "run", "tap", "--node", "resource_id=app:id/x", "--region", "0", "0", "1", ".5"])
    assert args.command == "teach" and args.teach_action == "tap" and args.node == "resource_id=app:id/x"
    args = cli.parser().parse_args(["teach", "run", "swipe", "--direction", "down", "--distance", ".3"])
    assert args.direction == "down" and args.distance == .3
    args = cli.parser().parse_args(["teach", "run", "interrupt", "--name", "x", "--detect-ocr", "新版本", "--back", "--entry", "xiaohe"])
    assert args.back and args.entry == "xiaohe"
    assert cli.parser().parse_args(["cases", "run"]).command == "cases"
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["teach", "run"])


def test_rules_command_reports_health(tmp_path, capsys):
    store = RuleStore(tmp_path / "rules")
    item = observation()
    store.promote(PACKAGE, "open", {"actions": [{"type": "tap", "target": {"kind": "node", "attr": "text", "value": "打开"}}]}, item, "agent")
    store.invalidate(PACKAGE, "open", "一次失败", 1, item.version)
    assert cli.main(["rules", "--rule-dir", str(tmp_path / "rules")]) == 0
    import json
    report = json.loads(capsys.readouterr().out)["rules"]
    assert report == [{"step": f"{PACKAGE}:open", "revision": 1, "active": True, "source": "agent", "kind": "actions",
                       "app_version": "1", "verified_versions": ["1"], "activity": "MainActivity", "successes": 0,
                       "failures": 1, "last_error": "一次失败", "invalidated_version": None}]


def test_teach_point_transient_and_lynx_semantic_summary(tmp_path, monkeypatch):
    from geo_collector.teach import summarize, teach as run_teach

    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)

    page = observation()
    page.nodes.extend([
        {"package": "com.android.systemui", "resource_id": "com.android.systemui:id/clock",
         "text": "20:00", "bounds": [5, 2, 45, 18], "clickable": True},
        {"package": PACKAGE, "resource_id": "", "text": "", "description": "进入按钮",
         "class_name": "com.lynx.tasm.behavior.ui.LynxFlattenUI", "bounds": [120, 100, 170, 140], "clickable": False},
        {"package": PACKAGE, "resource_id": "", "text": "重试", "description": "",
         "class_name": "android.widget.TextView", "bounds": [60, 180, 120, 220], "clickable": True},
    ])
    summary = summarize(page)
    assert any(c["description"] == "进入按钮" for c in summary["controls"])
    assert all("systemui" not in c["resource_id"] for c in summary["controls"])

    class TransientThenPointPhone(FakeDevice):
        serial = "phone-1"

        def tap(self, point, **kwargs):
            self.actions.append(tuple(point))
            if point == (90.0, 200.0):
                self.current = replace(page, nodes=[n for n in page.nodes if n.get("text") != "重试"])
            else:
                self.current = observation(ready=True)

    store, phone, run_dir = RuleStore(tmp_path / "rules"), TransientThenPointPhone(page), tmp_path / "run"
    state = run_state(serial="phone-1")
    request_file = stop(StepEngine(phone, store, run_dir, state))

    args_transient = cli.parser().parse_args(["teach", str(run_dir), "tap", "--node", "text=重试", "--transient"])
    first = run_teach(phone, store, run_dir, args_transient)
    assert first["transient"] is True and first["recorded"] is False and first["route_steps"] == 0
    assert read_json(request_file)["status"] == "transient"
    assert "exploration" not in read_json(run_dir / "调试产物/run.json")

    # Stop again on the recovered page and teach via --point on a whitelisted UI_LABELS control ("搜索").
    phone.current.nodes.append({"package": PACKAGE, "text": "搜索", "resource_id": "app:id/go",
                                "bounds": [120, 100, 170, 140], "clickable": True})
    stop(StepEngine(phone, store, run_dir, read_json(run_dir / "调试产物/run.json")))
    args_point = cli.parser().parse_args(["teach", str(run_dir), "tap", "--point", "145", "120"])
    second = run_teach(phone, store, run_dir, args_point)
    assert second["recorded"] is True and second["route_steps"] == 1
    assert second["action"]["target"]["value"] == "app:id/go"
