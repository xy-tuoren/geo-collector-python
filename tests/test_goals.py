"""Teach → persisted restart → replay → changed path → repair, plus role evidence."""
from dataclasses import replace
from pathlib import Path

import pytest

from geo_collector.capture import question_bubble, citation_state, product_state, answer_viewport
from geo_collector.goals import bind_roles, repair_context
from geo_collector.recovery import NeedsRepair, StepEngine, submit_repair
from geo_collector.rules import RuleStore
from geo_collector.storage import read_json, write_json
from test_recovery import observation, recipe, FakeDevice, PACKAGE, is_ready
from test_capture import FakeDevice as CapturePhone
from test_capture import FakeEngine as CaptureEngine


@pytest.mark.parametrize("entry", ["douyin", "toutiao"])
def test_search_loading_only_allows_readonly_resume_and_preserves_recovery_choices(tmp_path, entry):
    from geo_collector.teach import TeachSession

    state = {"entry": entry, "index": 1, "phase": "submitted", "tasks": [
        {"entry": entry, "index": 1, "text": "完整原题"}]}
    machine = StepEngine(FakeDevice(observation()), RuleStore(tmp_path / "rules"), tmp_path, state)
    with pytest.raises(NeedsRepair) as stopped:
        machine._request(entry + ".results_ready", "等待首屏加载", observation(), (), "加载中")
    file = Path(stopped.value.request)
    request = read_json(file)
    assert request["readonly"] and request["allowed_resolutions"] == ["resume"]
    for plan in (recipe(), {"perception": {"answer_page": recipe()["actions"][0]["target"]}}):
        with pytest.raises(ValueError, match="完整性复查"):
            submit_repair(file, plan=plan, sha256=request["sha256"])
    assert not machine.device.actions and not (file.parent / "response.json").exists()
    with pytest.raises(ValueError, match="只允许 resume"):
        TeachSession(machine.device, machine.store, tmp_path, state).perception("answer_page", recipe()["actions"][0]["target"])
    write_json(file.parent / "response.json", {"request_id": request["request_id"], "sha256": request["sha256"], "recipe": plan})
    with pytest.raises(ValueError, match="只读完整性复查"):
        machine._request(entry + ".results_ready", "等待首屏加载", observation(), (), "加载中")
    assert state["pending_request"] == str(file) and not machine.device.actions
    assert "recover.reenter-answer" in repair_context(state, "capture.review", "回答页丢失")["allowed_resolutions"]
    state["products_resume"] = {"phase": "list"}
    assert "recover.reenter-answer" not in repair_context(state, "capture.review", "药品采集未完成")["allowed_resolutions"]


def test_goal_learns_actual_multistep_path_then_repairs_changed_middle_without_replaying_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    class Phone(FakeDevice):
        def __init__(self, changed=False):
            super().__init__(observation())
            self.stage, self.changed = 0, changed

        def tap(self, point, **kwargs):
            self.actions.append(point)
            self.stage += 1
            if self.stage == 1:
                self.current = replace(observation(color="gray"), activity="Second", nodes=[
                    {"text": "新入口" if self.changed else "继续", "resource_id": "new" if self.changed else "next", "bounds": [20, 50, 90, 90]}])
            else:
                self.current = observation(ready=True)

    store = RuleStore(tmp_path / "rules")
    state = {"entry": "native", "index": 1, "phase": "open"}
    phone = Phone()
    machine = StepEngine(phone, store, tmp_path / "run", state)
    def run(engine):
        return engine.perform("goal.open", "进入目标页", PACKAGE, is_ready)
    def submit(engine, label):
        with pytest.raises(NeedsRepair) as stopped:
            run(engine)
        file = Path(stopped.value.request)
        plan = {**recipe(label), "explore": True}
        submit_repair(file, plan=plan, sha256=read_json(file)["sha256"])

    submit(machine, "打开")
    submit(machine, "继续")
    assert len(phone.actions) == 1
    assert store.get(PACKAGE, "goal.open") is None
    # A new Python process loads only its real JSON checkpoint, no live closures.
    machine = StepEngine(phone, store, tmp_path / "run", read_json(tmp_path / "run/调试产物/run.json"))
    assert is_ready(run(machine)) and len(phone.actions) == 2
    assert store.get(PACKAGE, "goal.open") is None, "a reached subgoal isn't a complete collection"
    machine.commit_flows()  # run_questions calls this only after complete artifact validation.
    saved = store.get(PACKAGE, "goal.open")
    assert len(saved["recipe"]["route"]) == 2

    replay = StepEngine(Phone(), store, tmp_path / "replay", {"entry": "native", "index": 2, "phase": "open"})
    assert is_ready(run(replay))
    assert len(replay.device.actions) == 2 and replay.state.get("agent_requests", 0) == 0

    changed = StepEngine(Phone(True), store, tmp_path / "changed", {"entry": "native", "index": 3, "phase": "open"})
    submit(changed, "新入口")
    assert len(changed.device.actions) == 1, "must stop before the now-invalid second action"
    assert is_ready(run(changed)) and len(changed.device.actions) == 2
    changed.commit_flows()
    assert len(store.get(PACKAGE, "goal.open", observation=observation())["recipe"]["route"]) == 2
    final = StepEngine(Phone(True), store, tmp_path / "final", {"entry": "native", "index": 4, "phase": "open"})
    assert is_ready(run(final)) and len(final.device.actions) == 2
    assert final.state.get("agent_requests", 0) == 0


@pytest.mark.parametrize("scale,logical_scale", [(1, 1.5), (2, 1)])
def test_learned_question_role_ignores_bubble_color_but_requires_exact_non_editor_text(scale, logical_scale):
    phone = CapturePhone(scale, logical_scale, references=False)
    original = phone.observe(True)
    from PIL import Image
    from io import BytesIO
    buffer = BytesIO()
    Image.new("RGB", original.physical_size, "white").save(buffer, "PNG")
    changed = replace(original, png=buffer.getvalue(), nodes=[*original.nodes,
        {"resource_id": "app:id/user_question", "package": original.package, "text": "", "class_name": "android.view.View",
         "bounds": [value * logical_scale for value in (90, 110, 340, 190)]}])
    viewport = answer_viewport(changed)
    assert question_bubble(changed, phone.question, viewport) is None
    plan = {"perception": {"question": {"kind": "node", "attr": "resource_id", "value": "app:id/user_question"}}}
    learned = bind_roles(plan, changed, require=True)
    assert question_bubble(learned, phone.question, viewport)["method"] == "learned_question_role_and_exact_ocr"
    assert question_bubble(learned, "different question", viewport) is None
    assert changed.png == learned.png and "collector_role" not in changed.nodes[-1]
    changed.nodes[-1]["class_name"] = "android.widget.EditText"
    with pytest.raises(RuntimeError, match="输入框"):
        bind_roles(plan, changed, require=True)


def test_page_roles_do_not_mark_unexpanded_references_or_products_complete():
    original = observation()
    original.nodes = [{"package": PACKAGE, "resource_id": "refs", "text": "证据来源", "bounds": [10, 70, 150, 100]},
                      {"package": PACKAGE, "resource_id": "meds", "text": "全部药品 >", "bounds": [10, 300, 150, 325]}]
    learned = bind_roles({"perception": {
        "citations_header": {"kind": "node", "attr": "resource_id", "value": "refs"},
        "products_entry": {"kind": "node", "attr": "resource_id", "value": "meds"},
    }}, original, require=True)
    refs = citation_state(learned, (0, 40, 180, 350))
    products = product_state(learned, (0, 40, 180, 350))
    assert refs["present"] and not refs["expanded"]
    assert products["present"] and not products["complete"]
    learned.nodes.append({"package": PACKAGE, "text": "这是普通的回答正文", "bounds": [10, 110, 150, 130], "collector_role": "citations_rows"})
    assert not citation_state(learned, (0, 40, 180, 350))["expanded"]
    learned.nodes[-1]["text"] = "慢性病诊疗指南（2025）"
    assert citation_state(learned, (0, 40, 180, 350))["expanded"]


@pytest.mark.parametrize("label", ["全部药品", "全部药品>", "全部药品 ›", "更多药品〉", "查看全部参考药品 →", "查看所有推荐药品》"])
@pytest.mark.parametrize("learned", [False, True])
def test_product_entry_accepts_real_list_labels_with_visible_arrows(label, learned):
    original = observation()
    original.nodes = [{"package": PACKAGE, "resource_id": "meds", "text": label, "bounds": [10, 300, 150, 325]}]
    current = bind_roles({"perception": {
        "products_entry": {"kind": "node", "attr": "resource_id", "value": "meds"},
    }}, original, require=True) if learned else original
    products = product_state(current, (0, 40, 180, 350))
    assert products["present"] and products["mode"] == "drawer" and not products["complete"]


@pytest.mark.parametrize("label", ["用药建议清单", "口服补液盐", "药物指南", "查看说明书", "加入购物车", "全部药品均应遵医嘱", "查看全部药品后再咨询"])
def test_learned_product_entry_cannot_relabel_unrelated_controls(label):
    original = observation()
    original.nodes = [{"package": PACKAGE, "resource_id": "meds", "text": label, "bounds": [10, 300, 150, 325]}]
    learned = bind_roles({"perception": {
        "products_entry": {"kind": "node", "attr": "resource_id", "value": "meds"},
    }}, original, require=True)
    assert learned.nodes[0]["text"] == label
    products = product_state(learned, (0, 40, 180, 350))
    assert not products["present"] and products["mode"] == "none"


def test_exploration_cannot_send_or_replay_uncertain_action(tmp_path):
    phone = FakeDevice(observation())
    phone.current.nodes[0]["text"] = "发送"
    machine = StepEngine(phone, RuleStore(tmp_path / "rules"), tmp_path, {"entry": "native", "index": 1, "phase": "capture"})
    with pytest.raises(NeedsRepair) as stopped:
        machine.perform("goal.open", "打开", PACKAGE, is_ready)
    request = read_json(Path(stopped.value.request))
    submit_repair(stopped.value.request, plan={**recipe("发送"), "explore": True}, sha256=request["sha256"])
    with pytest.raises(RuntimeError, match="发送"):
        machine.perform("goal.open", "打开", PACKAGE, is_ready)
    assert not phone.actions and not machine.store.all()["rules"]


def test_role_handoff_resumes_verified_preparation_without_generation_or_top_rescan(tmp_path):
    from geo_collector.capture import AnswerCapture
    from geo_collector.storage import write_json
    from itertools import count
    phone = CapturePhone(references=False)
    class Engine(CaptureEngine):
        def save(self):
            write_json(tmp_path / "state.json", self.state)
        def perception(self, current, reason):
            raise NeedsRepair(tmp_path / "teaching-request.json")
    first = Engine(phone)
    first.teach = {"page.perception"}
    capture = AnswerCapture(phone, first)
    ticks = count(0, .5)
    capture._clock, capture._sleep = lambda: next(ticks), lambda _: None
    with pytest.raises(NeedsRepair):
        capture.run(phone.question, phone.package, tmp_path / "out", tmp_path / "debug")
    assert "capture_preparation" in read_json(tmp_path / "state.json")
    second = Engine(phone)
    second.state = read_json(tmp_path / "state.json")
    second.teach = set()
    resumed = AnswerCapture(phone, second)
    resumed._clock, resumed._sleep = lambda: next(ticks), lambda _: None
    def forbidden(*args):
        raise AssertionError("verified preparation must not repeat")
    resumed._complete = resumed._top = forbidden
    result = resumed.run(phone.question, phone.package, tmp_path / "out", tmp_path / "debug")
    assert result["end_confirmed"] and result["question_confirmed"]
    assert "capture_preparation" not in second.state


def test_current_douyin_can_enter_exact_existing_search_without_input_send_or_launch(tmp_path, monkeypatch):
    from test_workflow import Phone, Capture, state
    from geo_collector import workflow
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin", mode="current")
    phone.stage, phone.question = "results", run["tasks"][0]["text"]
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert [call[0] for call in phone.calls] == ["tap", "capture"]


def test_capture_recovers_exact_question_by_observed_route_then_reuses_it_without_agent(tmp_path, monkeypatch):
    from io import BytesIO
    from PIL import Image, ImageDraw
    from geo_collector.capture import AnswerCapture

    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)

    class Phone(CapturePhone):
        def __init__(self):
            super().__init__(references=False)
            self.stage, self.taps = 0, []

        def observe(self, ocr=False):
            current = super().observe(ocr)
            if self.stage == 2:
                return current
            # Both intermediate pages have a real bubble, but it belongs to
            # another question. Navigation alone cannot satisfy the goal.
            current.texts[0]["text"] = "A different question"
            picture = Image.open(BytesIO(current.png)).convert("RGB")
            draw = ImageDraw.Draw(picture)
            draw.rectangle((110, 126, 335, 165), fill="#00bdb2")
            draw.text((120, 136), "A different question", fill="white")
            output = BytesIO()
            picture.save(output, "PNG")
            current.png = output.getvalue()
            current.nodes.append({"resource_id": "history_button" if self.stage == 0 else "current_answer",
                                  "package": self.package, "class_name": "android.widget.Button",
                                  "text": "历史回答" if self.stage == 0 else "回到本题",
                                  "bounds": [20, 20, 80, 60], "clickable": True, "enabled": True})
            if self.stage == 1:
                current.nodes = [node for node in current.nodes if not node.get("class_name", "").endswith("EditText")]
            return current

        def swipe(self, *args, **kwargs):
            offset = self.offset
            super().swipe(*args, **kwargs)
            if self.stage < 2:
                self.offset = offset

        def tap(self, point, *, expected_package):
            assert expected_package == self.package and point == (50, 40)
            self.taps.append(self.stage)
            self.stage += 1

    step = "capture.question_top"
    store = RuleStore(tmp_path / "rules")
    phone = Phone()
    run_dir = tmp_path / "run"

    def engine_for(device, directory, index):
        checkpoint = directory / "调试产物/run.json"
        state = read_json(checkpoint) if checkpoint.exists() else {
            "entry": "native", "index": index, "phase": "capture"}
        return StepEngine(device, store, directory, state)

    def collect(engine, directory):
        capture = AnswerCapture(engine.device, engine)
        clock = [0.0]
        capture._clock = lambda: clock[0]
        capture._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        return capture.run(engine.device.question, engine.device.package,
                           directory / "delivery", directory / "debug")

    machine = engine_for(phone, run_dir, 1)
    with pytest.raises(NeedsRepair) as stopped:
        collect(machine, run_dir)
    request_file = Path(stopped.value.request)
    assert read_json(request_file)["step"] == step
    initial_swipes = len(phone.swipes)

    for ordinal, anchor in enumerate(("history_button", "current_answer")):
        request = read_json(request_file)
        submit_repair(request_file, plan={"actions": [{"type": "tap", "target": {
            "kind": "node", "attr": "resource_id", "value": anchor}}], "explore": True}, sha256=request["sha256"])
        machine = engine_for(phone, run_dir, 1)
        if ordinal == 0:
            with pytest.raises(NeedsRepair) as stopped:
                collect(machine, run_dir)
            request_file = Path(stopped.value.request)
            assert read_json(request_file)["step"] == step
            assert phone.taps == [0] and len(phone.swipes) == initial_swipes
            assert not (run_dir / "delivery").exists()
        else:
            result = collect(machine, run_dir)
        assert store.get(phone.package, step) is None, "a subgoal must not publish its route"

    assert phone.taps == [0, 1], "resuming must not repeat the explored prefix"
    assert result["question_confirmed"] and result["end_confirmed"]
    events = (run_dir / "调试产物/events.jsonl").read_text(encoding="utf-8")
    assert events.count('"answer_generation_complete"') == 2
    machine.commit_flows(result)
    assert len(store.get(phone.package, step)["recipe"]["route"]) == 2

    fresh_phone, fresh_dir = Phone(), tmp_path / "fresh"
    fresh = engine_for(fresh_phone, fresh_dir, 2)
    repeated = collect(fresh, fresh_dir)
    assert repeated["question_confirmed"] and repeated["end_confirmed"]
    assert fresh_phone.taps == [0, 1]
    assert fresh.state.get("agent_requests", 0) == 0
    assert fresh.state.get("goal_route_replay_steps") == 2


def test_capture_review_accepts_page_role_before_rechecking_the_original_page_guard(tmp_path, monkeypatch):
    from geo_collector.capture import AnswerCapture
    from geo_collector.goals import role_nodes

    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)

    class Phone(CapturePhone):
        def observe(self, ocr=False):
            current = super().observe(ocr)
            current.nodes.append({"resource_id": "answer_brand", "text": "小荷AI医生", "package": self.package,
                                  "class_name": "android.widget.TextView", "bounds": [10, 10, 130, 40]})
            return current

    phone, store = Phone(references=False), RuleStore(tmp_path / "rules")
    machine = StepEngine(phone, store, tmp_path, {"entry": "native", "index": 1, "phase": "capture"})

    def collect(engine):
        capture = AnswerCapture(phone, engine, answer_page_check=lambda item: bool(role_nodes(item, "answer_page")))
        clock = [0.0]
        capture._clock = lambda: clock[0]
        capture._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        return capture.run(phone.question, phone.package, tmp_path / "delivery", tmp_path / "debug")

    with pytest.raises(NeedsRepair) as stopped:
        collect(machine)
    file = Path(stopped.value.request)
    request = read_json(file)
    assert request["step"] == "capture.review" and not phone.swipes
    submit_repair(file, plan={"perception": {"answer_page": {
        "kind": "node", "attr": "resource_id", "value": "answer_brand"}}}, sha256=request["sha256"])
    resumed = StepEngine(phone, store, tmp_path, read_json(tmp_path / "调试产物/run.json"))
    result = collect(resumed)
    assert result["question_confirmed"] and result["end_confirmed"]
    assert "pending_request" not in resumed.state
    assert read_json(file)["status"] == "candidate"
    assert store.get(phone.package, "perception.answer_page") is None


def test_capture_action_candidate_retains_its_request_and_cannot_replay_a_send_control(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    before = observation()
    before.nodes[0]["resource_id"] = "same_control"
    phone = FakeDevice(before, observation(ready=True))
    machine = StepEngine(phone, RuleStore(tmp_path / "rules"), tmp_path,
                         {"entry": "native", "index": 1, "phase": "capture"})
    step = "capture.question_top"

    def run():
        return machine.perform(step, "取得原题首屏", PACKAGE, is_ready)

    with pytest.raises(NeedsRepair) as stopped:
        run()
    file = Path(stopped.value.request)
    submit_repair(file, plan={"actions": [{"type": "tap", "target": {
        "kind": "node", "attr": "resource_id", "value": "same_control"}}]}, sha256=read_json(file)["sha256"])
    assert is_ready(run())
    phone.current = before
    assert is_ready(run())
    assert len(phone.actions) == 2
    assert machine.state["flow_candidates"][step]["request"] == str(file)
    assert machine.store.get(PACKAGE, step) is None

    before.nodes[0]["text"] = "发送"
    phone.current = before
    with pytest.raises(NeedsRepair):
        run()
    assert len(phone.actions) == 2, "an uncommitted Agent action is still subject to send protection"
