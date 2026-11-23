"""Toutiao entry identity, safe submission and first-screen recovery."""
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from geo_collector import cli, workflow
from geo_collector.capture import verify_capture_images
from geo_collector.device import DeviceError, LaunchForegroundError
from geo_collector.platforms import toutiao
from geo_collector.platforms.common import edit
from geo_collector.recovery import NeedsRepair, StepEngine, submit_repair
from geo_collector.rules import RuleStore, locate
from geo_collector.storage import read_json
from test_workflow import Capture, Phone, state
import test_capture as capture_samples


def sync_xml(observation):
    hierarchy = ET.Element("hierarchy")
    for node in observation.nodes:
        left, top, right, bottom = node["bounds"]
        ET.SubElement(hierarchy, "node", {
            "text": str(node.get("text", "")), "content-desc": node.get("description", ""),
            "resource-id": node.get("resource_id", ""), "class": node.get("class_name", ""),
            "hint": node.get("hint", ""), "package": node.get("package", observation.package),
            "bounds": f"[{left},{top}][{right},{bottom}]", "visible-to-user": "true",
            **{key: str(bool(node.get(key, key == "enabled"))).lower()
               for key in ("clickable", "focused", "scrollable", "enabled")},
        })
    observation.xml = ET.tostring(hierarchy, encoding="unicode")
    return observation


class ToutiaoPhone(Phone):
    def __init__(self, scale=1, card="summary", field="new"):
        super().__init__("toutiao", scale)
        self.card, self.field = card, field
        self.problem = None

    def observe(self, ocr=False):
        observation = super().observe(ocr)
        base = observation.nodes[0]

        def add(label, bounds, **attrs):
            observation.nodes.append({**base, "text": label, "class_name": "android.widget.TextView",
                                      "bounds": bounds, "clickable": True, **attrs})

        if self.stage == "home":
            search = observation.nodes[-1]
            search.update(resource_id=toutiao.PACKAGE + ":id/kic", description="搜索框，搜你想看的",
                          bounds=(20, 60, 610, 140))
        elif self.stage in {"search", "old", "results"}:
            field = next(node for node in observation.nodes if node["resource_id"].endswith(":id/et_search_kw"))
            field["resource_id"] = toutiao.PACKAGE + (":id/cx" if self.field == "old" else ":id/d0")
            if self.stage == "results" and not self.focused:
                field.update(text="搜索框，" + self.question, class_name="android.widget.TextView")
            search = next(node for node in observation.nodes if node["text"] == "搜索")
            search["resource_id"] = toutiao.PACKAGE + ":id/e0"
            if self.stage == "results":
                observation.nodes = [node for node in observation.nodes if node.get("text") not in {"小荷AI医生", "免费咨询"}]
                if self.card == "summary":
                    add("小荷AI医生·智能总结", (40, 420, 410, 470))
                    add("查看更多", (260, 550, 460, 610))
                elif self.card == "miniapp":
                    add("小荷AI医生", (40, 420, 350, 470))
                    add("小程序", (370, 420, 480, 470))
                if self.problem:
                    content = next(node for node in observation.nodes if node["text"].startswith("普通搜索结果"))
                    if self.problem == "blank":
                        observation.nodes.remove(content)
                    elif self.problem == "progress":
                        content["class_name"] = "android.widget.ProgressBar"
                    else:
                        content["text"] = "加载中" if self.problem == "loading" else "网络异常，请重试"
        elif self.stage == "chat":
            add("", (20, 1400, 680, 1500), class_name="android.widget.EditText", hint="发送消息")
        return sync_xml(observation)

    def launch(self, package, restart=False):
        assert package == toutiao.PACKAGE
        self.calls.append(("launch", package))
        self.stage, self.focused = "home", False

    def tap(self, point, expected_package):
        previous = self.stage
        super().tap(point, expected_package)
        if previous in {"search", "old"} and self.stage == "results":
            self.focused = False
            if self.crash_send:
                self.crash_send = False
                raise OSError("simulated connection loss after Toutiao search")

    def swipe(self, *args, **kwargs):
        raise AssertionError("Toutiao search must never scan lower viewports")


@pytest.fixture(autouse=True)
def fast_capture(monkeypatch):
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)


class RestoredToutiaoPhone(ToutiaoPhone):
    def __init__(self, foreign=False, broken_restart=False):
        super().__init__()
        self.foreign = foreign
        self.browser = not foreign
        self.broken_restart = broken_restart
        self.restarts = []
        self.stage = "home"

    def observe(self, ocr=False):
        observation = super().observe(ocr)
        if self.foreign:
            observation.package = "com.ss.android.ugc.aweme"
            observation.activity = observation.package + ".SearchResultActivity"
        elif self.browser:
            observation.activity = "com.ss.android.newmedia.activity.browser.BrowserActivity"
        return observation

    def launch(self, package, restart=False):
        super().launch(package, restart)
        self.restarts.append(restart)
        if restart and not self.broken_restart:
            self.foreign = self.browser = False


def test_restored_browser_restarts_before_any_cached_back_navigation(tmp_path):
    phone, run, store = RestoredToutiaoPhone(), state("toutiao"), RuleStore(tmp_path / "rules")
    store.promote(toutiao.PACKAGE, "toutiao.open_search", {"actions": [{"type": "back"}]}, phone.observe(), "agent")
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert phone.restarts == [False, True]
    assert not any(call[0] == "press" for call in phone.calls)
    assert sum(call[0] == "send" for call in phone.calls) == 1
    restart = run["tasks"][0]["entry_restart"]
    assert restart["status"] == "verified" and restart["reason"] == "restored_browser"
    assert all((Path(restart[key]) / "screen.png").exists() for key in ("before", "after"))


def test_resume_open_from_foreign_foreground_restores_without_rewinding(tmp_path):
    phone, run = RestoredToutiaoPhone(foreign=True), state("toutiao", phase="open")
    run["tasks"][0]["failure"] = "previous-navigation-evidence.json"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete" and phone.restarts == [True]
    assert run["tasks"][0]["failure"] == "previous-navigation-evidence.json"
    assert run["tasks"][0]["entry_restart"]["reason"] == "foreign_foreground"
    assert sum(call[0] == "input" for call in phone.calls) == 1
    assert sum(call[0] == "send" for call in phone.calls) == 1


@pytest.mark.parametrize("mode,phase", [("current", "open"), ("batch", "submitting"), ("batch", "submitted"), ("batch", "capture")])
def test_entry_normalization_never_restarts_after_submission_or_in_current_mode(tmp_path, mode, phase):
    phone, run = RestoredToutiaoPhone(foreign=True), state("toutiao", mode=mode, phase=phase)
    engine = StepEngine(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    toutiao._normalize_entry(engine, run["tasks"][0])
    assert not phone.calls and not phone.restarts


@pytest.mark.parametrize("key,value", [("pending_request", "unrelated-request.json"),
                                      ("inflight", {"step": "toutiao.send", "risk": "send"}),
                                      ("exploration", {"step": "toutiao.open_search", "dispatched": {"type": "back"}})])
def test_entry_restart_does_not_discard_pending_or_uncertain_evidence(tmp_path, key, value):
    phone, run = RestoredToutiaoPhone(foreign=True), state("toutiao", phase="open")
    run.update(entry="toutiao", index=1, phase="open", **{key: value})
    engine = StepEngine(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    with pytest.raises(DeviceError, match="待处理或不确定"):
        toutiao._normalize_entry(engine, run["tasks"][0])
    assert run[key] == value and not phone.calls


def test_failed_entry_restart_stops_before_navigation_and_is_bounded(tmp_path):
    phone, run = RestoredToutiaoPhone(foreign=True, broken_restart=True), state("toutiao", phase="open")
    store, run_dir = RuleStore(tmp_path / "rules"), tmp_path / "run"
    workflow.run_questions(phone, store, run_dir, run)
    assert run["status"] == "incomplete" and run["tasks"][0]["phase"] == "open"
    assert run["tasks"][0]["entry_restart"]["status"] == "failed"
    workflow.run_questions(phone, store, run_dir, run)
    assert phone.restarts == [True]
    assert not any(call[0] in {"tap", "press", "input", "send"} for call in phone.calls)
    restart = run["tasks"][0]["entry_restart"]
    assert len(restart["observations"]) == 12
    assert all((Path(path) / "observation.json").exists() for path in restart["observations"])


def test_delayed_launch_is_confirmed_with_readonly_observations_without_relaunch(tmp_path):
    class DelayedRestartPhone(RestoredToutiaoPhone):
        remaining = None

        def launch(self, package, restart=False):
            super().launch(package, restart)
            if restart:
                self.remaining = 2
                raise LaunchForegroundError("应用启动后前台不匹配，未执行后续操作。")

        def observe(self, ocr=False):
            if self.remaining is not None:
                if self.remaining == 0:
                    self.foreign = self.browser = False
                else:
                    self.remaining -= 1
            return super().observe(ocr)

    phone, run = DelayedRestartPhone(foreign=True, broken_restart=True), state("toutiao", phase="open")
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete" and phone.restarts == [True]
    restart = run["tasks"][0]["entry_restart"]
    assert restart["status"] == "verified" and restart["launch_error"]
    observations = [read_json(Path(path) / "observation.json") for path in restart["observations"]]
    assert [item["package"] for item in observations] == ["com.ss.android.ugc.aweme"] * 2 + [toutiao.PACKAGE]
    assert sum(call[0] == "send" for call in phone.calls) == 1


def test_resume_started_restart_records_confirmation_without_restarting(tmp_path):
    phone, run = RestoredToutiaoPhone(foreign=True, broken_restart=True), state("toutiao", phase="open")
    run["tasks"][0]["entry_restart"] = {"status": "started", "before": str(tmp_path / "restart" / "before")}
    phone.foreign = phone.browser = False
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete" and phone.restarts == []
    restart = run["tasks"][0]["entry_restart"]
    assert restart["status"] == "verified" and len(restart["observations"]) == 1


def test_entry_restart_does_not_swallow_other_device_errors(tmp_path):
    class LockedPhone(RestoredToutiaoPhone):
        def launch(self, package, restart=False):
            raise DeviceError("手机仍被锁定")

    phone, run = LockedPhone(foreign=True), state("toutiao", phase="open")
    run.update(entry="toutiao", index=1, phase="open")
    engine = StepEngine(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    with pytest.raises(DeviceError, match="手机仍被锁定"):
        toutiao._normalize_entry(engine, run["tasks"][0])
    assert not run["tasks"][0]["entry_restart"].get("observations")


@pytest.mark.parametrize("scale,field", [(1, "old"), (1.5, "new")])
@pytest.mark.parametrize("card", ["summary", "miniapp"])
def test_toutiao_full_run_and_verified_artifacts(tmp_path, scale, field, card):
    phone, run = ToutiaoPhone(scale, card, field), state("toutiao")
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    assert run["status"] == "complete"
    assert run["tasks"][0]["outcome"] == "captured"
    assert [call for call in phone.calls if call[0] == "send"] == [("send", phone.question)]
    assert cli.verify_run(run_dir)["complete"]


@pytest.mark.parametrize("scale,field", [(1, "old"), (1.5, "new")])
def test_search_identity_requires_matching_query_results_and_real_focused_editor(scale, field):
    phone = ToutiaoPhone(scale=scale, field=field)
    task = state("toutiao")["tasks"][0]
    phone.question, phone.stage, phone.focused = task["text"], "search", True
    observation = phone.observe()
    assert edit(observation, "toutiao", focused=True) is not None
    assert not toutiao.sent(observation, task)
    phone.stage, phone.focused = "results", False
    observation = phone.observe()
    assert edit(observation, "toutiao") is not None
    assert edit(observation, "toutiao", focused=True) is None
    assert toutiao.sent(observation, task)
    assert not toutiao.search_results_ready(observation, "另一道完整问题？")
    observation.package = "other.app"
    assert not toutiao.sent(observation, task)
    assert not toutiao.brand_entry_defaults(observation)


def test_noneditable_search_label_cannot_borrow_focus_from_another_editor():
    phone = ToutiaoPhone()
    phone.stage, phone.question = "results", "这是完整测试问题？"
    observation = phone.observe()
    header = next(node for node in observation.nodes if node["text"].startswith("搜索框，"))
    header["focused"] = True
    observation.nodes.append({**header, "text": "其他输入框", "bounds": (20, 1400, 680, 1500),
                              "class_name": "android.widget.EditText", "resource_id": ""})
    assert edit(observation, "toutiao", focused=True) is None


def test_focused_top_d0_semantic_search_field_is_editable():
    phone = ToutiaoPhone()
    phone.stage, phone.focused = "search", True
    observation = phone.observe()
    field = next(node for node in observation.nodes if node["resource_id"].endswith(":id/d0"))
    field.update(text="搜索框，已有搜索词", class_name="", clickable=True)
    selected = edit(observation, "toutiao", focused=True)
    assert selected is not None and selected["text"] == "已有搜索词"


def test_focused_top_d0_with_old_result_tabs_is_not_submitted():
    phone = ToutiaoPhone()
    task = state("toutiao")["tasks"][0]
    phone.stage, phone.question, phone.focused = "results", task["text"], True
    observation = phone.observe()
    field = next(node for node in observation.nodes if node["resource_id"].endswith(":id/d0"))
    field.update(text="搜索框，" + task["text"], class_name="", clickable=True)
    assert edit(observation, "toutiao", focused=True) is not None
    assert not toutiao.search_results_ready(observation, task["text"])
    assert not toutiao.sent(observation, task)


def test_learned_bottom_composer_is_never_a_toutiao_search_input():
    phone = ToutiaoPhone()
    phone.stage, phone.question = "chat", "这是完整测试问题？"
    observation = phone.observe()
    composer = next(node for node in observation.nodes if node["class_name"].endswith("EditText"))
    composer.update(collector_role="input", focused=True)
    assert edit(observation, "toutiao") is None
    assert edit(observation, "toutiao", focused=True) is None

    class SearchGuard:
        def perform(self, step, goal, package, postcondition, **options):
            assert step == "toutiao.open_search" and package == toutiao.PACKAGE
            assert not postcondition(observation)

    toutiao.open(SearchGuard(), state("toutiao")["tasks"][0])


def test_editing_query_over_old_results_is_not_proof_of_submission():
    phone = ToutiaoPhone()
    task = state("toutiao")["tasks"][0]
    phone.stage, phone.question, phone.focused = "results", task["text"], True
    assert edit(phone.observe(), "toutiao", focused=True) is not None
    assert not toutiao.sent(phone.observe(), task)


@pytest.mark.parametrize("scale", [1, 1.5])
def test_native_summary_accepts_its_full_width_view_more_button(tmp_path, scale):
    phone = ToutiaoPhone(scale=scale)
    phone.stage, phone.question = "results", "这是完整测试问题？"
    observation = phone.observe()
    button = next(node for node in observation.nodes if node["text"] == "查看更多")
    button.update(bounds=(48, 550, 672, 640), class_name="android.widget.Button")
    recipes = toutiao.brand_entry_defaults(observation)
    assert recipes
    assert locate(recipes[0]["actions"][0]["target"], observation, tmp_path) == pytest.approx((360, 595))


@pytest.mark.parametrize("scale", [1, 1.5])
@pytest.mark.parametrize("card", ["summary", "miniapp"])
def test_entry_ocr_targets_map_physical_screenshots_to_logical_taps(tmp_path, scale, card):
    phone = ToutiaoPhone(scale=scale, card=card)
    phone.stage, phone.question = "results", "这是完整测试问题？"
    observation = phone.observe()
    entry_labels = {"小荷AI医生·智能总结", "查看更多", "小荷AI医生", "小程序"}
    matching = [node for node in observation.nodes if node.get("text") in entry_labels]
    observation.texts = [{"text": node["text"], "confidence": .99,
                          "bounds": tuple(round(value * scale) for value in node["bounds"])} for node in matching]
    observation.nodes = [node for node in observation.nodes if node not in matching]
    recipes = toutiao.brand_entry_defaults(observation)
    assert recipes and toutiao.platform_entry_visible(observation)
    targets = [recipe["actions"][-1]["target"] for recipe in recipes]
    assert all(target["kind"] == "ocr" for target in targets)
    expected = (360, 580) if card == "summary" else (195, 445)
    assert any(locate(target, observation, tmp_path) == pytest.approx(expected) for target in targets)


@pytest.mark.parametrize("problem", ["unbranded", "distant", "ambiguous"])
def test_summary_does_not_click_unrelated_or_ambiguous_view_more_buttons(tmp_path, problem):
    phone = ToutiaoPhone()
    phone.stage, phone.question = "results", "这是完整测试问题？"
    observation = phone.observe()
    title = next(node for node in observation.nodes if node["text"] == "小荷AI医生·智能总结")
    button = next(node for node in observation.nodes if node["text"] == "查看更多")
    if problem == "unbranded":
        title["text"] = "普通智能总结"
    elif problem == "distant":
        button["bounds"] = (260, 1450, 460, 1510)
    else:
        observation.nodes.append({**button, "bounds": (260, 620, 460, 680)})
    assert toutiao.brand_entry_defaults(observation) == []


def test_current_answer_never_launches_inputs_or_sends(tmp_path):
    phone, run = ToutiaoPhone(), state("toutiao", mode="current")
    phone.stage, phone.question = "chat", run["tasks"][0]["text"]
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert phone.calls == [("capture", phone.question)]


@pytest.mark.parametrize("scale", [1, 1.5])
def test_answer_page_requires_branded_content_structure_and_accepts_legacy_webview(scale):
    phone = ToutiaoPhone(scale=scale)
    phone.stage, phone.question = "chat", "这是完整测试问题？"
    observation = phone.observe()
    assert toutiao.answer_ready(observation)
    original = list(observation.nodes)
    observation.nodes = [node for node in original if node is original[0] or node.get("text") == "小荷AI医生"
                         or node.get("description") == "关闭"]
    assert not toutiao.answer_ready(observation), "a branded shell alone is not an answer page"
    observation.nodes = original
    observation.activity = toutiao.PACKAGE + ".MainActivity"
    title = next(node for node in observation.nodes if node.get("text") == "小荷AI医生")
    title["resource_id"] = toutiao.PACKAGE + ":id/title"
    close = next(node for node in observation.nodes if node.get("description") == "关闭")
    close.update(description="返回，按钮", bounds=(0, 30, 90, 100))
    composer = next(node for node in observation.nodes if node["class_name"].endswith("EditText"))
    composer["hint"] = "输入问题AI免费专业解答"
    observation.nodes.extend([
        {**original[0], "text": "小荷AI医生", "class_name": "android.webkit.WebView", "bounds": (0, 110, 720, 1550)},
        {**original[0], "text": "AI生成非医疗诊断，仅供参考", "bounds": (0, 110, 720, 150)},
    ])
    assert toutiao.answer_ready(observation)
    observation.nodes.remove(title)
    assert not toutiao.answer_ready(observation), "an ordinary WebView must not be accepted as Xiaohe"


def test_uncertain_search_resumes_by_reading_without_repeating_send(tmp_path):
    phone, run, store = ToutiaoPhone(), state("toutiao"), RuleStore(tmp_path / "rules")
    phone.crash_send = True
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert read_json(Path(stopped.value.request))["uncertain_send"] is True
    assert run["tasks"][0]["phase"] == "submitting"
    previous = len(phone.calls)
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert not any(call[0] in {"launch", "input", "send"} for call in phone.calls[previous:])
    assert sum(call[0] == "send" for call in phone.calls) == 1


def test_loaded_first_screen_without_entry_finishes_and_verifies(tmp_path):
    phone, run = ToutiaoPhone(card=None), state("toutiao")
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    assert run["status"] == "complete" and run["tasks"][0]["outcome"] == "entry_not_found"
    assert not any(call[0] == "capture" for call in phone.calls)
    result = read_json(Path(run["tasks"][0]["result"]))
    assert result["scan_scope"]["viewports_checked"] == result["scan_scope"]["max_viewports"] == 1
    verified = cli.verify_run(run_dir)
    assert verified["complete"] and verified["captured"] == 0 and verified["entry_not_found"] == 1


@pytest.mark.parametrize("problem", ["blank", "loading", "progress", "network"])
def test_unloaded_result_is_readonly_and_resumes_without_resending(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(toutiao, "RESULT_READY_TIMEOUT", 0)
    phone, run, store = ToutiaoPhone(card=None), state("toutiao"), RuleStore(tmp_path / "rules")
    phone.problem = problem
    run_dir = tmp_path / "run"
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, store, run_dir, run)
    request = read_json(Path(stopped.value.request))
    assert request["step"] == "toutiao.results_ready" and request["readonly"]
    assert request["allowed_resolutions"] == ["resume"] and not request["uncertain_send"]
    assert "outcome" not in run["tasks"][0] and not (run_dir / "交付图片").exists()
    phone.problem, phone.card = None, "summary"
    workflow.run_questions(phone, store, run_dir, run)
    assert run["status"] == "complete"
    assert sum(call[0] == "send" for call in phone.calls) == 1
    assert sum(call[0] == "capture" for call in phone.calls) == 1


def test_taught_entry_is_promoted_after_capture_and_reused_for_a_fresh_question(tmp_path):
    phone, run, store = ToutiaoPhone(), state("toutiao"), RuleStore(tmp_path / "rules")
    run_dir = tmp_path / "first"
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, store, run_dir, run, teach=("toutiao.open_xiaohe",))
    request = read_json(Path(stopped.value.request))
    assert request["step"] == "toutiao.open_xiaohe"
    submit_repair(stopped.value.request, sha256=request["sha256"], plan={"actions": [{
        "type": "tap", "target": {"kind": "node", "attr": "text", "value": "查看更多"}}]})
    workflow.run_questions(phone, store, run_dir, run)
    assert run["status"] == "complete"
    assert store.get(toutiao.PACKAGE, "toutiao.open_xiaohe")["source"] == "agent"
    fresh = state("toutiao", question="第二道完整测试问题？")
    workflow.run_questions(phone, store, tmp_path / "second", fresh)
    assert fresh["status"] == "complete" and fresh["agent_requests"] == 0
    assert sum(call[0] == "send" for call in phone.calls) == 2


@pytest.mark.parametrize("scale,logical_scale", [(1, 1), (2, 1.5)])
@pytest.mark.parametrize("products", [False, True])
def test_real_shared_capture_validates_toutiao_body_and_terminal_medicines(tmp_path, monkeypatch, scale, logical_scale, products):
    # The shared synthetic drawer asserts its package even while expanded.
    monkeypatch.setattr(capture_samples, "PACKAGE", toutiao.PACKAGE)
    base = capture_samples.DrawerDevice if products else capture_samples.FakeDevice

    class CurrentToutiaoAnswer(base):
        body_swipes_when_drawer_open = None

        def observe(self, ocr=False):
            observation = super().observe(ocr)
            observation.activity = toutiao.PACKAGE + ".MiniAppHostActivity"
            for text, description, box in [("小荷AI医生", "", (100, 20, 260, 50)), ("", "关闭", (320, 20, 350, 50))]:
                observation.nodes.append({"text": text, "description": description, "class_name": "android.widget.TextView",
                                          "package": self.package, "clickable": True,
                                          "bounds": tuple(round(value * logical_scale) for value in box)})
            if getattr(self, "drawer", False) and self.body_swipes_when_drawer_open is None:
                self.body_swipes_when_drawer_open = len(self.swipes)
            return sync_xml(observation)

    device = CurrentToutiaoAnswer(scale, logical_scale)
    capture, engine = capture_samples.fast_capture(device)
    capture.answer_page_check = toutiao.answer_ready
    capture.answer_page_error = toutiao.ANSWER_PAGE_ERROR
    assert toutiao.answer_ready(device.observe())
    delivery, debug = tmp_path / "delivery", tmp_path / "debug"
    result = capture.run(device.question, toutiao.PACKAGE, delivery, debug)
    verify_capture_images(result, debug, delivery)
    assert result["question_confirmed"] and result["completion_confirmed"] and result["end_confirmed"]
    assert result["frame_count"] > 1 and all(seam["verified"] for seam in result["seams"])
    assert result["geometry"]["logical_size"] == [round(360 * logical_scale), round(800 * logical_scale)]
    if products:
        medicine = result["products"]
        assert all(medicine[key] for key in ("complete", "first_item_confirmed", "images_ready", "end_confirmed", "seams_verified", "terminal_sequence"))
        assert medicine["frame_count"] > 1 and medicine["post_scan_swipes"] == 0
        assert not medicine["restore_required"] and device.opens == 1
        assert len(device.swipes) == device.body_swipes_when_drawer_open
    else:
        assert result["citations_expanded"] and not result["products"]["present"]
