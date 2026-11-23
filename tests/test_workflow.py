from pathlib import Path
import io
import random
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw
import pytest

from geo_collector.device import Observation
from geo_collector.recovery import NeedsRepair, save_observation, submit_repair
from geo_collector.rules import RuleStore, locate
from geo_collector.storage import read_json, write_json
from geo_collector import cli, workflow
from geo_collector.platforms import douyin, xiaohe


class Phone:
    serial = "test-device"

    def __init__(self, entry="xiaohe", scale=1):
        self.entry, self.scale = entry, scale
        self.stage = "old"
        self.question = ""
        self.focused = False
        self.calls = []
        self.crash_send = False
        self.capture_error = False
        self.result_page = 0
        self.xiaohe_page = 0
        self.unlocatable_entry = False
        self.crash_scroll_page = None

    def observe(self, ocr=False):
        package = workflow.PACKAGES[self.entry]
        lw, lh = 720, 1600
        pw, ph = round(lw * self.scale), round(lh * self.scale)
        image = Image.new("RGB", (pw, ph), "white")
        nodes, texts = [], []

        def node(label, bounds, *, kind="android.widget.TextView", description="", resource="", focused=False, scrollable=False):
            value = {"text": label, "description": description, "resource_id": resource, "class_name": kind, "bounds": bounds,
                     "focused": focused, "enabled": True, "clickable": True, "scrollable": scrollable, "package": package}
            nodes.append(value)
            return value

        def text(label, bounds):
            texts.append({"text": label, "confidence": .99, "bounds": tuple(round(n * self.scale) for n in bounds)})

        node("", (0, 0, 720, 1600), kind="android.widget.FrameLayout")
        activity = package + ".MainActivity"
        if self.entry == "xiaohe":
            node("小荷AI医生", (220, 30, 470, 80))
            node("", (610, 30, 700, 90), description="开启新会话")
            node(self.question if self.stage != "chat" else "", (20, 1400, 600, 1500), kind="android.widget.EditText", focused=self.focused)
            node("", (620, 1400, 700, 1500), description="发送")
            if self.stage == "new":
                node("有任何健康问题，都可以随时问我", (100, 400, 620, 450))
            if self.stage == "old":
                node("旧回答", (40, 250, 680, 340))
        else:
            if self.stage in {"miniapp_old", "chat"}:
                activity = package + ".MiniAppHostActivity"
                node("小荷AI医生", (220, 30, 470, 80))
                node("", (640, 30, 700, 80), description="关闭")
                node("输入问题", (20, 1400, 680, 1500))
            elif self.stage == "home":
                node("", (630, 40, 710, 120), description="搜索")
            else:
                node(self.question, (20, 90, 610, 150), kind="android.widget.EditText", resource=package + ":id/et_search_kw", focused=self.focused)
                node("搜索", (630, 90, 710, 150))
                if self.stage == "results":
                    node("综合", (30, 190, 140, 230))
                    node("视频", (180, 190, 280, 230))
                    node(f"普通搜索结果{self.result_page}", (40, 300, 500, 350))
                    canvas = Image.new("RGB", (pw, round(6200 * self.scale)), "white")
                    draw = ImageDraw.Draw(canvas)
                    rng = random.Random(20260915)
                    for _ in range(180):
                        top = rng.randrange(20, 6100)
                        shade = rng.randrange(20, 230)
                        draw.rectangle(tuple(round(n * self.scale) for n in (rng.randrange(0, 180), top, rng.randrange(520, 720), top + rng.randrange(10, 36))),
                                       fill=(shade, shade, shade))
                    offset = round(self.result_page * 720 * self.scale)
                    image = canvas.crop((0, offset, pw, offset + ph))
                    if self.xiaohe_page == self.result_page:
                        if not self.unlocatable_entry:
                            node("", (20, 390, 700, 620), kind="android.widget.FrameLayout")
                        node("小荷AI医生", (40, 420, 350, 470))
                        node("免费咨询", (490, 500, 660, 560))
        if self.stage == "chat":
            node("", (0, 160, 720, 1350), kind="android.widget.ScrollView", scrollable=True)
            ImageDraw.Draw(image).rectangle(tuple(round(n * self.scale) for n in (150, 210, 710, 310)), fill=(40, 200, 160))
            text(self.question, (170, 240, 690, 280))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        hierarchy = ET.Element("hierarchy")
        for value in nodes:
            left, top, right, bottom = value["bounds"]
            ET.SubElement(hierarchy, "node", {
                "text": str(value.get("text") or ""), "content-desc": str(value.get("description") or ""),
                "resource-id": str(value.get("resource_id") or ""), "class": str(value.get("class_name") or ""),
                "bounds": f"[{round(left)},{round(top)}][{round(right)},{round(bottom)}]",
                "clickable": str(bool(value.get("clickable"))).lower(),
                "scrollable": str(bool(value.get("scrollable"))).lower(),
                "package": str(value.get("package") or ""), "focused": str(bool(value.get("focused"))).lower(),
                "enabled": str(value.get("enabled", True)).lower(), "visible-to-user": "true",
            })
        xml = ET.tostring(hierarchy, encoding="unicode")
        return Observation(buffer.getvalue(), xml, package, activity, (lw, lh), (pw, ph), "test-1", nodes, texts)

    def launch(self, package, restart=False):
        self.calls.append(("launch", package))
        self.entry = next(key for key, value in workflow.PACKAGES.items() if value == package)
        self.stage = "old" if self.entry == "xiaohe" else "miniapp_old"
        self.focused = False

    def tap(self, point, expected_package):
        assert expected_package == workflow.PACKAGES[self.entry]
        x, y = point
        self.calls.append(("tap", tuple(point)))
        if self.entry == "xiaohe":
            if y < 100:
                self.stage, self.question, self.focused = "new", "", False
            elif x < 610:
                self.focused = True
            else:
                self.calls.append(("send", self.question))
                self.stage, self.focused = "chat", False
                if self.crash_send:
                    self.crash_send = False
                    raise OSError("simulated connection loss after send")
        elif self.stage == "miniapp_old":
            self.stage = "home"
        elif self.stage == "home":
            self.stage = "search"
        elif self.stage == "results" and y > 390:
            self.stage = "chat"
        elif x < 620:
            self.focused = True
        else:
            self.calls.append(("send", self.question))
            self.stage, self.focused = "results", True

    def input_text(self, text, expected_package, clear=True):
        assert self.focused and expected_package == workflow.PACKAGES[self.entry] and clear is True
        self.calls.append(("input", text))
        self.question = text

    def press(self, key, expected_package):
        self.calls.append(("press", key))
        self.stage = "home"

    def screenshot(self):
        return self.observe().png

    def swipe(self, start, end, duration=0.4, *, expected_package):
        assert self.entry == "douyin" and self.stage == "results"
        assert expected_package == workflow.PACKAGES["douyin"] and start[1] > end[1]
        self.calls.append(("swipe", self.result_page))
        self.result_page += 1
        if self.result_page == self.crash_scroll_page:
            self.crash_scroll_page = None
            raise OSError("simulated connection loss after result scroll")


class Capture:
    def __init__(self, device, engine, **_):
        self.device = device

    def run(self, question, package, delivery, debug):
        if self.device.capture_error:
            raise RuntimeError("simulated incomplete seam")
        assert self.device.question == question
        assert workflow.exact_question_visible(self.device.observe(), question)
        self.device.calls.append(("capture", question))
        Path(delivery).mkdir(parents=True, exist_ok=True)
        output = Path(delivery) / "answer.png"
        output.write_bytes(self.device.screenshot())
        frame = Path(debug) / "capture-test" / "frame-001.png"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(output.read_bytes())
        return {"question_confirmed": True, "citations_checked": True, "citations_found": False, "citations_expanded": False,
                "completion_confirmed": True, "end_confirmed": True, "occlusion_checked": True, "frame_count": 1, "seams": [], "image_paths": [str(output)],
                "frame_files": [str(frame)], "products": {"present": False, "complete": True}}


@pytest.fixture(autouse=True)
def capture_and_fast_wait(monkeypatch):
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)


def state(entry="xiaohe", question="这是完整测试问题？", mode="batch", phase="start"):
    return {"entries": [entry], "questions": [{"index": 1, "text": question}], "tasks": [{"entry": entry, "index": 1, "text": question, "status": "pending", "phase": phase}],
            "cursor": 0, "serial": "test-device", "status": "running", "mode": mode, "agent_requests": 0}


@pytest.mark.parametrize("entry,scale", [("xiaohe", 1), ("xiaohe", 1.5), ("douyin", 1), ("douyin", 1.5)])
def test_full_native_and_douyin_submission_with_original_question_identity(tmp_path, entry, scale):
    phone = Phone(entry, scale)
    run = state(entry)
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert run["tasks"][0]["phase"] == "done"
    assert [call for call in phone.calls if call[0] == "send"] == [("send", run["tasks"][0]["text"])]
    result = read_json(tmp_path / "run" / "调试产物" / entry / "001" / "answer.json")
    assert (result["entry"], result["index"], result["question"]) == (entry, 1, run["tasks"][0]["text"])


def test_teach_send_pauses_before_submitting_and_saved_rule_replays_without_agent(tmp_path):
    phone, store = Phone(), RuleStore(tmp_path / "rules")
    run = state()
    run_dir = tmp_path / "first"
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, store, run_dir, run, teach=("xiaohe.send",))
    assert run["phase"] == run["tasks"][0]["phase"] == "input"
    assert not any(call[0] == "send" for call in phone.calls)
    request = read_json(Path(stopped.value.request))
    assert request["uncertain_send"] is False
    submit_repair(stopped.value.request, sha256=request["sha256"], plan={"actions": [{"type": "tap", "target": {"kind": "node", "attr": "description", "value": "发送"}}]})
    workflow.run_questions(phone, store, run_dir, run)
    assert run["status"] == "complete"
    assert store.get(workflow.PACKAGES["xiaohe"], "xiaohe.send")["source"] == "agent"
    fresh = state(question="第二道完整问题？")
    workflow.run_questions(phone, store, tmp_path / "second", fresh)
    assert fresh["status"] == "complete" and fresh["agent_requests"] == 0
    assert len([call for call in phone.calls if call[0] == "send"]) == 2


def test_resume_after_uncertain_send_only_confirms_without_sending_twice(tmp_path):
    phone, store, run = Phone(), RuleStore(tmp_path / "rules"), state()
    phone.crash_send = True
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["phase"] == "submitting"
    saved = read_json(tmp_path / "run" / "调试产物" / "run.json")
    assert saved["phase"] == saved["tasks"][0]["phase"] == "submitting"
    before = list(phone.calls)
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert not any(call[0] in {"send", "input", "launch"} for call in phone.calls[len(before):])
    assert len([call for call in phone.calls if call[0] == "send"]) == 1


def test_uncertain_send_without_exact_bubble_stops_at_same_question(tmp_path):
    phone, run = Phone(), state(phase="submitting")
    phone.stage, phone.question, phone.focused = "new", run["tasks"][0]["text"], True
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert phone.calls == []
    assert run["cursor"] == 0 and run["phase"] == "submitting"
    assert read_json(Path(stopped.value.request))["uncertain_send"] is True
    assert not workflow.exact_question_visible(phone.observe(), phone.question), "matching text in the editor is not proof of submission"


def test_current_answer_mode_never_launches_focuses_inputs_or_sends(tmp_path):
    phone, run = Phone(), state(mode="current")
    phone.stage, phone.question = "chat", run["tasks"][0]["text"]
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert phone.calls == [("capture", phone.question)]
    assert run["status"] == "complete"


@pytest.mark.parametrize("missing_proof", ["missing_occlusion", "unchecked_occlusion", "boolean_frame_count"])
def test_capture_without_valid_occlusion_or_frame_proof_cannot_complete(tmp_path, monkeypatch, missing_proof):
    class UnprovenCapture(Capture):
        def run(self, *args):
            result = super().run(*args)
            if missing_proof == "missing_occlusion":
                result.pop("occlusion_checked")
            elif missing_proof == "unchecked_occlusion":
                result["occlusion_checked"] = False
            else:
                result["frame_count"] = True
            return result

    monkeypatch.setattr(workflow, "AnswerCapture", UnprovenCapture)
    phone, run = Phone(), state(mode="current")
    phone.stage, phone.question = "chat", run["tasks"][0]["text"]
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "incomplete"
    assert run["cursor"] == 0 and run["tasks"][0]["phase"] == "capture"
    assert phone.calls == [("capture", phone.question)]
    debug = tmp_path / "run" / "调试产物" / "xiaohe" / "001"
    assert (debug / "failure.json").exists()
    assert not (debug / "answer.json").exists()


def test_current_mode_failure_preserves_diagnostics_without_submission(tmp_path):
    phone, run = Phone(), state(mode="current")
    phone.stage, phone.question, phone.capture_error = "chat", run["tasks"][0]["text"], True
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "incomplete"
    assert phone.calls == []
    debug = tmp_path / "run" / "调试产物" / "xiaohe" / "001"
    assert (debug / "failure.json").exists()
    assert (debug / "failure" / "screen.png").exists()
    assert not (debug / "answer.json").exists()
    assert run["cursor"] == 0 and run["tasks"][0]["phase"] == "capture"
    phone.capture_error = False
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert phone.calls == [("capture", phone.question)]


def test_failed_capture_stops_before_later_question_and_resume_never_resends(tmp_path):
    phone, store, run = Phone(), RuleStore(tmp_path / "rules"), state()
    run["tasks"].append({"entry": "xiaohe", "index": 2, "text": "第二题不能提前发送？", "status": "pending", "phase": "start"})
    phone.capture_error = True
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["cursor"] == 0 and run["tasks"][0]["phase"] == "capture"
    assert run["tasks"][1]["status"] == "pending"
    assert len([call for call in phone.calls if call[0] == "send"]) == 1
    phone.capture_error = False
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert [call[1] for call in phone.calls if call[0] == "send"] == [task["text"] for task in run["tasks"]]


def test_next_native_question_closes_completed_product_drawer_before_new_session(tmp_path, monkeypatch):
    class ProductPhone(Phone):
        drawer_open = False

        def observe(self, ocr=False):
            observation = super().observe(ocr=ocr)
            if self.drawer_open:
                observation.activity += ".ProductDrawer"
            return observation

        def press(self, key, expected_package):
            assert key == "back" and expected_package == workflow.PACKAGES["xiaohe"]
            assert self.drawer_open
            self.calls.append(("close_products", self.question))
            self.drawer_open = False

        def tap(self, point, expected_package):
            assert not self.drawer_open, "close the previous drawer before navigating or typing"
            if point[1] < 100:
                self.calls.append(("new_session", self.question))
            super().tap(point, expected_package)

    class ProductCapture(Capture):
        def run(self, *args):
            result = super().run(*args)
            self.device.drawer_open = True
            return result

    monkeypatch.setattr(xiaohe, "drawer_state", lambda observation: {} if observation.activity.endswith(".ProductDrawer") else None)
    monkeypatch.setattr(workflow, "AnswerCapture", ProductCapture)
    phone, run = ProductPhone(), state()
    run["tasks"].append({"entry": "xiaohe", "index": 2, "text": "第二道完整问题？", "status": "pending", "phase": "start"})
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    actions = [call[0] for call in phone.calls if call[0] in {"new_session", "send", "capture", "close_products"}]
    assert actions == ["new_session", "send", "capture", "close_products", "new_session", "send", "capture"]
    assert phone.drawer_open, "the last capture terminates at products without restoring the answer body"


def test_search_history_and_unbranded_consult_buttons_are_not_result_evidence():
    phone = Phone("douyin")
    phone.stage, phone.question = "search", "这是完整测试问题？"
    observation = phone.observe()
    assert not workflow.search_results_ready(observation, phone.question)
    phone.stage = "results"
    observation = phone.observe()
    assert workflow.search_results_ready(observation, phone.question)
    assert not workflow.search_results_ready(observation, "另一个问题？")
    assert workflow._brand_entry_defaults(observation)
    observation.nodes = [node for node in observation.nodes if node.get("text") != "小荷AI医生"]
    assert workflow._brand_entry_defaults(observation) == []


@pytest.mark.parametrize("message", ["思考中...", "生成中...", "正在生成...", "正在生成回复", "正在生成回答"])
def test_douyin_branded_result_waits_for_generation(message):
    phone = Phone("douyin")
    phone.stage, phone.question = "results", "这是完整测试问题？"
    observation = phone.observe()
    observation.texts.append({"text": message, "confidence": .99, "bounds": [120, 360, 360, 410]})
    assert not douyin.results_loaded(observation, phone.question)


def test_distant_brand_and_generic_cta_are_not_treated_as_one_xiaohe_card():
    phone = Phone("douyin")
    phone.stage, phone.question, phone.xiaohe_page = "results", "这是完整测试问题？", None
    observation = phone.observe()
    base = observation.nodes[0]
    observation.nodes.extend([
        {**base, "text": "小荷AI医生", "bounds": [40, 300, 350, 350]},
        {**base, "text": "免费咨询", "bounds": [490, 1200, 660, 1260]},
    ])
    assert not douyin.platform_entry_visible(observation)
    assert not douyin.brand_entry_defaults(observation)


def test_douyin_ocr_summary_card_with_arrow_is_a_safe_entry(tmp_path):
    phone = Phone("douyin")
    phone.stage, phone.question, phone.xiaohe_page = "results", "这是完整测试问题？", None
    observation = phone.observe()
    base = observation.nodes[0]
    observation.nodes.append({**base, "text": "", "bounds": [20, 260, 700, 810]})
    observation.texts.extend([
        {"text": "小荷AI医生", "confidence": .99, "bounds": [122, 286, 265, 319]},
        {"text": "字节跳动旗下医疗大模型应用", "confidence": .99, "bounds": [123, 327, 429, 355]},
        {"text": "查看全文>", "confidence": .99, "bounds": [292, 747, 424, 780]},
    ])
    recipes = douyin.brand_entry_defaults(observation)
    assert douyin.platform_entry_visible(observation) and len(recipes) == 1
    target = recipes[0]["actions"][0]["target"]
    assert target["kind"] == "ocr"
    assert locate(target, observation, tmp_path) == pytest.approx((358, 763.5))

    observation.texts.pop()
    assert douyin.platform_entry_visible(observation)
    assert douyin.brand_entry_defaults(observation) == []

    observation.texts.append({"text": "免费咨询", "confidence": .99, "bounds": [292, 747, 424, 780]})
    assert douyin.platform_entry_visible(observation)
    assert douyin.brand_entry_defaults(observation) == []

    observation.texts[-1]["text"] = "查看全文>"
    observation.nodes[-1]["bounds"] = [20, 260, 700, 700]
    assert douyin.platform_entry_visible(observation)
    assert douyin.brand_entry_defaults(observation) == []


def test_editing_query_while_old_result_tabs_remain_does_not_skip_new_search(tmp_path):
    phone, run = Phone("douyin"), state("douyin", phase="input")
    phone.stage, phone.focused, phone.question = "results", True, run["tasks"][0]["text"]
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert [call for call in phone.calls if call[0] == "send"] == [("send", phone.question)]


def test_douyin_first_screen_without_entry_finishes_without_scrolling(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    task = run["tasks"][0]
    assert run["status"] == "complete" and task["phase"] == "done"
    assert task["outcome"] == "entry_not_found" and "answer" not in task
    assert not any(call[0] == "swipe" for call in phone.calls)
    assert not any(call[0] == "capture" for call in phone.calls)
    result = read_json(Path(task["result"]))
    assert result["scan_scope"] == {"type": "first_screen", "max_viewports": 1, "viewports_checked": 1,
                                     "limit_reached": True, "end_confirmed": False}
    assert len(result["observation_paths"]) == 1 and run.get("agent_requests", 0) == 0
    verified = cli.verify_run(run_dir)
    assert verified["complete"] and verified["captured"] == 0 and verified["entry_not_found"] == 1
    assert cli.main(["verify", str(run_dir)]) == 0


def test_verify_accepts_byte_exact_crlf_entry_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)

    class CrLfPhone(Phone):
        def observe(self, ocr=False):
            observation = super().observe(ocr)
            observation.xml = observation.xml.replace("><", ">\r\n<")
            return observation

    phone, run = CrLfPhone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    assert cli.verify_run(run_dir)["complete"]


def test_douyin_does_not_search_lower_viewports_even_if_they_have_an_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.xiaohe_page = 2
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["tasks"][0]["outcome"] == "entry_not_found"
    assert not any(call[0] in {"swipe", "capture"} for call in phone.calls)


def test_old_scan_checkpoint_reuses_original_first_screen_without_more_actions(tmp_path):
    phone, run = Phone("douyin"), state("douyin", phase="submitted")
    phone.stage, phone.question, phone.xiaohe_page = "results", run["tasks"][0]["text"], None
    run_dir, store = tmp_path / "run", RuleStore(tmp_path / "rules")
    paths = []
    for ordinal in range(2):
        phone.result_page = ordinal
        directory = run_dir / "调试产物/douyin/001/entry-scan" / f"{ordinal + 1:03d}"
        save_observation(directory, phone.observe())
        paths.append(str(directory.resolve()))
    run["entry_scan_resume"] = {"question": phone.question, "package": douyin.PACKAGE, "entry": "douyin", "index": 1,
                                "max_viewports": 5, "observation_paths": paths.copy(), "swipe_inflight": {"after_observations": 2}}
    workflow.run_questions(phone, store, run_dir, run)
    result = read_json(Path(run["tasks"][0]["result"]))
    assert result["scan_scope"]["viewports_checked"] == 1
    assert result["observation_paths"] == paths[:1]
    assert not phone.calls and Path(paths[1]).is_dir()
    assert "entry_scan_resume" not in run and cli.verify_run(run_dir)["complete"]


def test_douyin_completed_scan_survives_exit_before_task_result_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir, store = tmp_path / "run", RuleStore(tmp_path / "rules")
    finish = workflow._finish_entry_outcome

    def exit_between_scan_and_result(*_):
        assert run["entry_scan_resume"]["scan_complete"] is True
        raise SystemExit("simulated process exit")

    monkeypatch.setattr(workflow, "_finish_entry_outcome", exit_between_scan_and_result)
    with pytest.raises(SystemExit):
        workflow.run_questions(phone, store, run_dir, run)
    swipes = len([call for call in phone.calls if call[0] == "swipe"])
    monkeypatch.setattr(workflow, "_finish_entry_outcome", finish)
    workflow.run_questions(phone, store, run_dir, run)
    assert len([call for call in phone.calls if call[0] == "swipe"]) == swipes == 0
    assert run["tasks"][0]["outcome"] == "entry_not_found" and cli.verify_run(run_dir)["complete"]


@pytest.mark.parametrize("problem", ["blank", "loading", "generating", "progress", "network"])
def test_unloaded_first_screen_pauses_then_finds_late_entry_without_resending(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(douyin, "RESULT_READY_TIMEOUT", 0)

    class LoadingPhone(Phone):
        unavailable = True

        def observe(self, ocr=False):
            observation = super().observe(ocr)
            if self.stage == "results" and self.unavailable:
                content = next(node for node in observation.nodes if node["text"].startswith("普通搜索结果"))
                if problem == "blank":
                    observation.nodes.remove(content)
                elif problem == "progress":
                    content["class_name"] = "android.widget.ProgressBar"
                elif problem == "generating":
                    content["text"] = "生成中..."
                else:
                    content["text"] = "加载中" if problem == "loading" else "网络异常，请重试"
            return observation

    phone, run = LoadingPhone("douyin"), state("douyin")
    phone.xiaohe_page = 0 if problem == "generating" else None
    run_dir, store = tmp_path / "run", RuleStore(tmp_path / "rules")
    with pytest.raises(NeedsRepair) as stopped:
        workflow.run_questions(phone, store, run_dir, run)
    request = read_json(Path(stopped.value.request))
    assert request["step"] == "douyin.results_ready" and request["readonly"]
    assert request["allowed_resolutions"] == ["resume"] and not request["uncertain_send"]
    assert "outcome" not in run["tasks"][0] and not (run_dir / "交付图片").exists()
    assert not any(call[0] == "swipe" for call in phone.calls)
    phone.unavailable, phone.xiaohe_page = False, 0
    workflow.run_questions(phone, store, run_dir, run)
    assert run["tasks"][0]["outcome"] == "captured"
    assert [call[0] for call in phone.calls].count("send") == 1
    assert [call[0] for call in phone.calls].count("capture") == 1


def test_douyin_animation_without_scroll_does_not_count_as_new_viewports(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)

    class AnimatedNoScrollPhone(Phone):
        animation_frame = 0

        def observe(self, ocr=False):
            observation = super().observe(ocr)
            if self.stage == "results":
                image = Image.open(io.BytesIO(observation.png)).convert("RGB")
                self.animation_frame += 1
                ImageDraw.Draw(image).rectangle((30, 520, 150, 650), fill=(30 + self.animation_frame % 4 * 40, 60, 120))
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                observation.png = buffer.getvalue()
            return observation

        def swipe(self, start, end, duration=0.4, *, expected_package):
            assert self.stage == "results" and start[1] > end[1]
            self.calls.append(("swipe", self.result_page))

    phone, run = AnimatedNoScrollPhone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    result = read_json(Path(run["tasks"][0]["result"]))
    assert result["scan_scope"]["viewports_checked"] == 1
    assert result["scan_scope"]["end_confirmed"] is False
    assert not any(call[0] == "swipe" for call in phone.calls)
    assert cli.verify_run(run_dir)["complete"]


def test_verify_still_accepts_historical_completed_five_viewport_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    result_file = Path(run["tasks"][0]["result"])
    result = read_json(result_file)
    for index in range(1, 5):
        phone.result_page = index
        directory = result_file.parent / "entry-scan" / f"{index + 1:03d}"
        save_observation(directory, phone.observe())
        result["observation_paths"].append(str(directory.resolve()))
    result["scan_scope"] = {"max_viewports": 5, "viewports_checked": 5, "limit_reached": True, "end_confirmed": False}
    write_json(result_file, result)
    assert cli.verify_run(run_dir)["complete"]


def test_douyin_entry_not_found_continues_with_the_next_question(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    class MixedPhone(Phone):
        def input_text(self, text, expected_package, clear=True):
            super().input_text(text, expected_package, clear)
            self.result_page = 0
            self.xiaohe_page = None if text.startswith("第一题") else 0

    phone, run = MixedPhone("douyin"), state("douyin", "第一题没有入口？")
    run["tasks"].append({"entry": "douyin", "index": 2, "text": "第二题存在入口？", "status": "pending", "phase": "start"})
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert [task["outcome"] for task in run["tasks"]] == ["entry_not_found", "captured"]
    assert [call[0] for call in phone.calls].count("send") == 2
    assert [call[0] for call in phone.calls].count("capture") == 1


def test_visible_douyin_brand_without_safe_card_locator_still_requests_repair(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.unlocatable_entry = True
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["tasks"][0]["status"] == "needs_repair"
    assert not any(call[0] in {"swipe", "capture"} for call in phone.calls)


@pytest.mark.parametrize("mutation", ["missing_xml", "invalid_png", "unloaded", "wrong_query", "wrong_package",
                                       "forged_nodes", "duplicate_path", "delivery_png"])
def test_verify_rejects_invalid_entry_not_found_evidence(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr("geo_collector.platforms.common.time.sleep", lambda _: None)
    phone, run = Phone("douyin"), state("douyin")
    phone.xiaohe_page = None
    run_dir = tmp_path / "run"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), run_dir, run)
    result_file = Path(run["tasks"][0]["result"])
    result = read_json(result_file)
    first = Path(result["observation_paths"][0])
    if mutation == "missing_xml":
        (first / "screen.xml").unlink()
    elif mutation == "invalid_png":
        (first / "screen.png").write_bytes(b"not a png")
    elif mutation in {"wrong_query", "wrong_package", "forged_nodes", "unloaded"}:
        observation = read_json(first / "observation.json")
        hierarchy = ET.fromstring(observation["xml"])
        if mutation == "wrong_query":
            next(node for node in hierarchy if node.get("resource-id", "").endswith(":id/et_search_kw")).set("text", "另一题")
            next(node for node in observation["nodes"] if node.get("resource_id", "").endswith(":id/et_search_kw"))["text"] = "另一题"
        elif mutation == "wrong_package":
            observation["package"] = "com.example.foreign"
            for node in hierarchy:
                node.set("package", "com.example.foreign")
            for node in observation["nodes"]:
                node["package"] = "com.example.foreign"
        elif mutation == "unloaded":
            next(node for node in hierarchy if node.get("text", "").startswith("普通搜索结果")).set("class", "android.widget.ProgressBar")
        else:
            hierarchy.remove(next(node for node in hierarchy if node.get("resource-id", "").endswith(":id/et_search_kw")))
        observation["xml"] = ET.tostring(hierarchy, encoding="unicode")
        (first / "screen.xml").write_text(observation["xml"], encoding="utf-8")
        write_json(first / "observation.json", observation)
    elif mutation == "duplicate_path":
        result["observation_paths"].append(result["observation_paths"][0])
        write_json(result_file, result)
    else:
        output = run_dir / "交付图片" / "douyin" / "001" / "回答_001.png"
        output.parent.mkdir(parents=True)
        Image.new("RGB", (100, 100), "white").save(output)
    assert not cli.verify_run(run_dir)["complete"]


def native_home(scale, greeting=False):
    phone = Phone(scale=scale)
    phone.stage = "new"
    observation = phone.observe()
    if not greeting:
        observation.nodes = [node for node in observation.nodes if "有任何健康问题" not in node.get("text", "")]
    for label, bounds in [("不选咨询人", (30, 120, 210, 165)), ("报告解读", (30, 1300, 170, 1360))]:
        observation.nodes.append({**observation.nodes[0], "text": label, "class_name": "android.widget.Button", "bounds": bounds})
    image = Image.open(io.BytesIO(observation.png)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for bounds in [(30, 120, 210, 165), (30, 1300, 170, 1360), *([(100, 400, 620, 450)] if greeting else [])]:
        draw.rectangle(tuple(round(value * scale) for value in bounds), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    observation.png = buffer.getvalue()
    return observation


@pytest.mark.parametrize("scale", [.75, 1.5])
@pytest.mark.parametrize("greeting", [False, True])
def test_clean_native_session_supports_blank_and_greeting_home_at_different_scales(scale, greeting):
    assert workflow.clean_native_session(native_home(scale, greeting))


@pytest.mark.parametrize("scale", [.75, 1.5])
@pytest.mark.parametrize("evidence", ["node", "ocr", "bubble", "sparse_indicator", "edge_content"])
def test_clean_native_session_rejects_old_content_even_with_empty_editor_and_greeting(scale, evidence):
    observation = native_home(scale, greeting=True)
    if evidence == "node":
        observation.nodes.append({**observation.nodes[0], "text": "旧回答正文", "bounds": (30, 600, 650, 700)})
    elif evidence == "ocr":
        observation.texts.append({"text": "旧问题", "confidence": .99, "bounds": tuple(round(value * scale) for value in (300, 600, 650, 640))})
    else:
        image = Image.open(io.BytesIO(observation.png)).convert("RGB")
        box = {"bubble": (200, 600, 700, 680), "sparse_indicator": (350, 1000, 354, 1004), "edge_content": (1, 800, 5, 820)}[evidence]
        ImageDraw.Draw(image).rectangle(tuple(round(value * scale) for value in box), fill=(40, 200, 160))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        observation.png = buffer.getvalue()
    assert not workflow.clean_native_session(observation)


@pytest.mark.parametrize("problem", ["missing_editor", "multiple_editors", "nonempty_editor", "wrong_package", "missing_header"])
def test_clean_native_session_requires_target_header_and_unique_empty_editor(problem):
    observation = native_home(1)
    editor = workflow._edit(observation, "xiaohe")
    if problem == "missing_editor":
        observation.nodes.remove(editor)
    elif problem == "multiple_editors":
        observation.nodes.append(dict(editor))
    elif problem == "nonempty_editor":
        editor["text"] = "尚未发送的问题"
    elif problem == "wrong_package":
        observation.package = "some.other.app"
    else:
        observation.nodes = [node for node in observation.nodes if node.get("text") != "小荷AI医生"]
    assert not workflow.clean_native_session(observation)
