"""Direct mini-app discovery, session identity and at-most-once submission."""
import io
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from geo_collector import workflow
from geo_collector.capture import answer_viewport, direct_question_bubble, latest_user_bubble, question_bubble
from geo_collector.images import CaptureError
from geo_collector.device import DeviceError, Observation
from geo_collector.platforms import douyin_direct as direct
from geo_collector.recovery import NeedsRepair, StepEngine
from geo_collector.rules import RuleStore, locate
from geo_collector.storage import read_json
from test_workflow import Capture, state


class DirectPhone:
    serial = "test-device"

    def __init__(self, scale=1, stage="launcher"):
        self.scale, self.stage = scale, stage
        self.question, self.query, self.draft = "", "", ""
        self.focused = False
        self.calls = []
        self.capture_error = self.crash_send = self.crash_confirm = False
        self.marker = True
        self.old_same_question = False
        self.show_session_label = True
        self.arrow_only = False
        self.extra_card = False

    def observe(self, ocr=False):
        size = (720, 1600)
        physical = tuple(round(n * self.scale) for n in size)
        image = Image.new("RGB", physical, "white")
        draw = ImageDraw.Draw(image)
        nodes, texts = [], []

        def node(label, box, **kw):
            value = {"text": label, "description": "", "resource_id": "", "bounds": box,
                     "class_name": "android.widget.TextView", "enabled": True, "clickable": True,
                     "focused": False, "scrollable": False, "package": direct.PACKAGE, **kw}
            nodes.append(value)
            return value

        def text(label, box):
            texts.append({"text": label, "bounds": tuple(round(n * self.scale) for n in box), "confidence": .99})

        node("", (0, 0, 720, 1600), class_name="android.widget.FrameLayout")
        activity = direct.PACKAGE + ".MainActivity"
        if self.stage == "launcher":
            node("搜索", (630, 50, 710, 110))
        elif self.stage in {"search", "results"}:
            node(self.query, (20, 70, 610, 140), class_name="android.widget.EditText",
                 resource_id=direct.PACKAGE + ":id/et_search_kw", focused=self.focused)
            node("搜索", (630, 70, 710, 140))
            if self.stage == "results":
                node("综合", (20, 180, 120, 220))
                node("视频", (180, 180, 280, 220))
                for shift in (0, 400) if self.extra_card else (0,):
                    node("", (20, 340 + shift, 700, 580 + shift), class_name="android.widget.FrameLayout")
                    node("小程序", (40, 360 + shift, 140, 400 + shift))
                    text("小荷AI医生", (60, 440 + shift, 300, 480 + shift))
                    node("进入", (560, 430 + shift, 670, 490 + shift))
        else:
            activity = "com.tt.miniapphost.placeholder.MiniAppHostActivity0"
            node("", (636, 80, 706, 140), description="关闭", class_name="android.widget.ImageView",
                 resource_id=direct.PACKAGE + ":id/close")
            if self.stage == "home":
                text("小荷AI医生", (170, 420, 550, 480))
                text("健康问题，随时问我", (150, 650, 570, 720))
                text("首页", (150, 1450, 230, 1500))
                text("我的", (500, 1450, 580, 1500))
            else:
                text("小荷AI医生", (30, 90, 240, 130))
                if self.show_session_label:
                    node("新会话", (380, 85, 450, 140))
                node("", (0, 170, 720, 1370), class_name="android.widget.ScrollView", scrollable=True)
                node("", (20, 1400, 700, 1530), class_name="android.widget.FrameLayout")
                node(self.draft or "发送消息", (134, 1448, 586, 1493), class_name="android.widget.EditText",
                     hint="发送消息", focused=self.focused)
                if not self.arrow_only:
                    node("发送", (620, 1440, 690, 1510))
                else:
                    cx, cy, arrow_size = 646, 1480, 54
                    line = [(cx, cy + arrow_size * .2), (cx, cy - arrow_size * .2)]
                    head = [(cx - arrow_size * .13, cy - arrow_size * .08),
                            (cx, cy - arrow_size * .2), (cx + arrow_size * .13, cy - arrow_size * .08)]
                    draw.line([tuple(round(v * self.scale) for v in p) for p in line], fill="black", width=max(2, round(4 * self.scale)))
                    draw.line([tuple(round(v * self.scale) for v in p) for p in head], fill="black", width=max(2, round(4 * self.scale)))
                if self.stage == "dialog":
                    text("开启新会话", (220, 600, 500, 660))
                    text("开启新会话后，当前咨询将自动结束", (90, 690, 630, 740))
                    node("确定", (390, 780, 600, 850))
                else:
                    text("上一题回答", (40, 250, 500, 310))
                    if self.stage in {"clean", "answer"} and self.marker:
                        text("已开始新会话", (260, 500, 460, 540))
                    if self.stage == "answer":
                        top = 250 if self.old_same_question else 650
                        draw.rectangle(tuple(round(v * self.scale) for v in (150, top, 710, top + 100)), fill=(40, 200, 160))
                        text(self.question, (170, top + 30, 690, top + 70))
                        text("这是已完成生成的实际回答正文。", (40, 850, 600, 910))
        hierarchy = ET.Element("hierarchy")
        for value in nodes:
            l, t, r, b = value["bounds"]
            ET.SubElement(hierarchy, "node", {
                "text": value["text"], "content-desc": value["description"], "resource-id": value["resource_id"],
                "class": value["class_name"], "package": value["package"], "hint": value.get("hint", ""),
                "bounds": f"[{l},{t}][{r},{b}]", "visible-to-user": "true",
                **{key: str(bool(value[key])).lower() for key in ("enabled", "clickable", "focused", "scrollable")},
            })
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return Observation(buffer.getvalue(), ET.tostring(hierarchy, encoding="unicode"), direct.PACKAGE,
                           activity, size, physical, "test", nodes, texts)

    def launch(self, package, restart=False):
        assert package == direct.PACKAGE and not restart
        self.calls.append(("launch", package))

    def tap(self, point, expected_package):
        assert expected_package == direct.PACKAGE
        self.calls.append(("tap", tuple(point)))
        x, y = point
        if self.stage == "launcher":
            self.stage = "search"
        elif self.stage == "search":
            if x < 620:
                self.focused = True
            else:
                self.stage, self.focused = "results", False
                self.calls.append(("name_search", self.query))
        elif self.stage == "results":
            self.stage = "home"
        elif self.stage == "home":
            self.stage = "chat"
        elif self.stage == "dialog":
            self.stage, self.draft, self.focused = "clean", "", False
            self.calls.append(("new_session",))
            if self.crash_confirm:
                self.crash_confirm = False
                raise OSError("disconnect after confirmation")
        elif y < 200:
            self.stage = "dialog"
        elif x < 610:
            self.focused = True
        else:
            self.question = self.draft
            self.stage, self.draft, self.focused = "answer", "", False
            self.calls.append(("send", self.question))
            if self.crash_send:
                self.crash_send = False
                raise OSError("disconnect after sending")

    def input_text(self, text, expected_package, clear=True):
        assert self.focused and expected_package == direct.PACKAGE and clear
        self.calls.append(("input", text))
        if self.stage == "search":
            self.query = text
        else:
            self.draft = text

    def swipe(self, *args, **kwargs):
        self.calls.append(("swipe",))
        self.marker = True

    def screenshot(self):
        return self.observe().png

    def recognize(self, observation):
        return observation


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setitem(workflow.PLATFORMS, direct.ID, direct)
    monkeypatch.setitem(workflow.PACKAGES, direct.ID, direct.PACKAGE)
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    original = direct._SubmissionProbe.__init__
    def fast_probe(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._sleep = lambda _: None
        self.quiet_seconds, self.generation_timeout_seconds = 0, 15
    monkeypatch.setattr(direct._SubmissionProbe, "__init__", fast_probe)


@pytest.mark.parametrize("scale", [1, 1.5])
def test_named_discovery_new_session_and_exact_single_submission(tmp_path, scale):
    phone, run = DirectPhone(scale), state(direct.ID)
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert [c for c in phone.calls if c[0] == "name_search"] == [("name_search", direct.QUERY)]
    assert [c for c in phone.calls if c[0] == "send"] == [("send", run["tasks"][0]["text"])]
    assert [c for c in phone.calls if c[0] == "new_session"] == [("new_session",)]
    assert run["agent_requests"] == 0


def test_existing_branded_chat_skips_discovery(tmp_path):
    phone, run = DirectPhone(stage="answer"), state(direct.ID)
    phone.question = "上一道问题"
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert not any(c[0] == "name_search" for c in phone.calls)


def test_resume_after_actual_send_never_repeats_input_or_send(tmp_path):
    phone, run = DirectPhone(), state(direct.ID)
    phone.crash_send = True
    store = RuleStore(tmp_path / "rules")
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["tasks"][0]["phase"] == "submitting"
    before = list(phone.calls)
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert not any(c[0] in {"input", "send", "new_session", "name_search"} for c in phone.calls[len(before):])


def test_resume_confirmation_does_not_open_another_session(tmp_path):
    phone, run = DirectPhone(), state(direct.ID)
    phone.crash_confirm = True
    store = RuleStore(tmp_path / "rules")
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["tasks"][0]["direct_open"]["stage"] == "session_confirm"
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert len([c for c in phone.calls if c[0] == "new_session"]) == 1


@pytest.mark.parametrize("scale", [1, 1.5])
def test_named_card_requires_unique_brand_section_button_and_exact_query(tmp_path, scale):
    phone = DirectPhone(scale, "results")
    phone.query = direct.QUERY
    observation = phone.observe()
    defaults = direct.named_entry_defaults(observation)
    assert len(defaults) == 1
    assert locate(defaults[0]["actions"][0]["target"], observation, tmp_path) == (615, 460)
    phone.extra_card = True
    assert direct.named_entry_defaults(phone.observe()) == []
    phone.extra_card = False
    phone.query = "小荷AI医生其他视频"
    assert direct.named_entry_defaults(phone.observe()) == []


def test_no_generic_ai_or_foreign_miniapp_is_accepted():
    phone = DirectPhone(stage="clean")
    observation = phone.observe()
    observation.package = "other.app"
    assert not direct.chat_state(observation)
    observation = phone.observe()
    observation.texts = [t for t in observation.texts if "小荷" not in t["text"]]
    assert not direct.chat_state(observation)


def test_new_session_tail_allows_old_history_but_rejects_new_messages_and_other_draft():
    phone = DirectPhone(stage="clean")
    assert direct.clean_session(phone.observe())
    phone.stage, phone.question = "answer", "本会话已经有问题"
    assert not direct.clean_session(phone.observe())
    phone.stage, phone.draft = "clean", "用户未发出的另一个草稿"
    assert not direct.clean_session(phone.observe(), "我要采集的问题")


def test_submitted_identity_rejects_old_same_question_and_missing_boundary():
    phone = DirectPhone(stage="answer")
    task = {"text": "同一个问题", "direct_open": {"session_confirmed": True}}
    phone.question = task["text"]
    assert direct.sent(phone.observe(), task)
    phone.old_same_question = True
    assert not direct.sent(phone.observe(), task)
    phone.old_same_question, phone.marker = False, False
    assert not direct.sent(phone.observe(), task)


def test_unrelated_draft_is_not_overwritten(tmp_path):
    phone, run = DirectPhone(stage="clean"), state(direct.ID, phase="input")
    phone.draft = "用户的其他草稿"
    run["tasks"][0]["direct_open"] = {"stage": "ready", "session_confirmed": True}
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "incomplete"
    assert not any(c[0] in {"input", "send"} for c in phone.calls)


@pytest.mark.parametrize("scale", [1, 1.5])
def test_unlabelled_new_session_icon_uses_verified_brand_anchor(tmp_path, scale):
    phone = DirectPhone(scale, "chat")
    phone.show_session_label = False
    observation = phone.observe()
    recipe = direct._new_session_defaults(observation)[0]
    point = locate(recipe["actions"][0]["target"], observation, tmp_path)
    assert 720 * .48 < point[0] < 720 * .69 and point[1] == 110


@pytest.mark.parametrize("scale", [1, 1.5])
def test_unlabelled_send_requires_up_arrow_inside_real_input_panel(scale):
    phone = DirectPhone(scale, "clean")
    phone.draft, phone.focused, phone.arrow_only = "测试问题", True, True
    target = direct.send_target(phone.observe())
    assert target is not None and target["method"] == "up_arrow_pixels"
    assert 620 <= sum(target["bounds"][::2]) / 2 <= 680
    observation = phone.observe()
    blank = io.BytesIO()
    Image.new("RGB", observation.physical_size, "white").save(blank, "PNG")
    observation.png = blank.getvalue()
    assert direct.send_target(observation) is None


def test_goal_protocol_reports_real_session_and_submission_conditions():
    phone, task = DirectPhone(stage="dialog"), state(direct.ID)["tasks"][0]
    assert direct.goal_check(direct.ID + ".new_session", task)(phone.observe())
    phone.stage = "clean"
    assert direct.goal_check(direct.ID + ".session_boundary", task)(phone.observe())
    assert not direct.goal_check(direct.ID + ".send", task)(phone.observe())
    assert direct.goal_check("other.step", task) is None


@pytest.mark.parametrize("scale", [1, 1.5])
def test_named_card_ignores_nested_body_and_duplicate_native_ocr_section(tmp_path, scale):
    phone = DirectPhone(scale, "results")
    phone.query = direct.QUERY
    observation = phone.observe()
    section = next(n for n in observation.nodes if n["text"] == "小程序")
    section["bounds"] = (40, 360, 680, 400)
    observation.texts.append({"text": "小程序", "bounds": tuple(round(v * scale) for v in (32, 361, 134, 398)), "confidence": .99})
    observation.nodes.append({**observation.nodes[0], "bounds": (20, 420, 700, 550)})
    recipes = direct.named_entry_defaults(observation)
    assert len(recipes) == 1
    assert locate(recipes[0]["actions"][0]["target"], observation, tmp_path) == (615, 460)


@pytest.mark.parametrize("scale", [1, 1.5])
def test_returning_user_home_accepts_continue_consultation_with_brand_and_greeting(scale):
    phone = DirectPhone(scale, "home")
    observation = phone.observe()
    for item in observation.texts:
        if item["text"] == "小荷AI医生":
            item["bounds"] = tuple(round(v * scale) for v in (85, 74, 244, 110))
        elif item["text"] == "健康问题，随时问我":
            item.update(text="继续咨询AI医生", bounds=tuple(round(v * scale) for v in (70, 766, 288, 802)))
    observation.texts.append({"text": "Hi 我是小荷AI医生", "bounds": tuple(round(v * scale) for v in (205, 566, 514, 607)), "confidence": .99})
    target = direct.home_target(observation)
    assert target is not None and target["label"] == "继续咨询AI医生"
    assert direct.goal_check(direct.ID + ".open_miniapp", state(direct.ID)["tasks"][0])(observation)
    observation.texts = [v for v in observation.texts if not v["text"].startswith("Hi")]
    assert direct.home_target(observation) is None


class ScrolledSubmissionPhone(DirectPhone):
    """Model the MiniApp scrolling the question away while answering."""

    _document_cache = {}
    _viewport_png_cache = {}

    def __init__(self, scale=1):
        super().__init__(scale)
        self.position, self.latest_question, self.no_boundary = None, None, False
        self.read_scrolls = []

    @classmethod
    def _document(cls, scale, no_boundary):
        key = (scale, no_boundary)
        document = cls._document_cache.get(key)
        if document is None:
            document = Image.new("RGB", (720, 3000), "white")
            ink, rng = ImageDraw.Draw(document), random.Random(471)
            for y in range(180, 2900, 28):
                for x in range(30, 700, 20):
                    ink.rectangle((x, y, x + rng.randrange(4, 15), y + rng.randrange(5, 16)), fill=(45, 45, 45))
            if not no_boundary:
                ink.rectangle((150, 650, 710, 750), fill=(40, 200, 160))
            document = document.resize((round(720 * scale), round(3000 * scale)), Image.Resampling.NEAREST)
            cls._document_cache[key] = document
        return document

    def observe(self, ocr=False):
        if self.position is None:
            return super().observe(ocr)
        if self.position == "question" and not self.no_boundary:
            original = self.question
            if self.latest_question is not None:
                self.question = self.latest_question
            result = super().observe(ocr)
            self.question = original
        else:
            stage, self.stage = self.stage, "clean"
            result = super().observe(ocr)
            self.stage = stage
            result.texts = [line for line in result.texts if line["text"] not in {"已开始新会话", "上一题回答"}]
            result.texts.append({"text": "这是当前会话已经完成的正文，正在显示回答的末尾。", "confidence": .99,
                                 "bounds": tuple(round(v * self.scale) for v in (40, 400, 650, 450))})
        # These are real overlapping pixel viewports of one document, not
        # unrelated stage screenshots. Navigation seams are verified normally.
        source = {"middle": 600, "bottom": 800, "question": 0}[self.position]
        png_key = (self.scale, self.no_boundary, self.position, result.png)
        rendered = self._viewport_png_cache.get(png_key)
        if rendered is None:
            document = self._document(self.scale, self.no_boundary)
            canvas = Image.open(io.BytesIO(result.png))
            canvas.paste(document.crop(tuple(round(v * self.scale) for v in (0, 170 + source, 720, 1370 + source))),
                         (0, round(170 * self.scale)))
            output = io.BytesIO()
            canvas.save(output, "PNG")
            rendered = output.getvalue()
            self._viewport_png_cache[png_key] = rendered
        result.png = rendered
        return result

    def swipe(self, start, end, duration=.4, *, expected_package):
        assert expected_package == direct.PACKAGE
        self.calls.append(("swipe",))
        self.read_scrolls.append("up" if end[1] > start[1] else "down")
        self.position = "question" if end[1] > start[1] else "bottom"


def interrupted_submission(tmp_path, phone):
    run, store = state(direct.ID), RuleStore(tmp_path / "rules")
    phone.crash_send = True
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["tasks"][0]["phase"] == "submitting"
    phone.position = "middle"
    return run, store


@pytest.mark.parametrize("scale", [1, 1.5])
def test_scrolled_uncertain_send_finds_latest_question_without_replaying(tmp_path, scale):
    phone = ScrolledSubmissionPhone(scale)
    run, store = interrupted_submission(tmp_path, phone)
    before = len(phone.calls)
    workflow.run_questions(phone, store, tmp_path / "run", run)
    assert run["status"] == "complete"
    assert phone.read_scrolls[:2] == ["down", "down"]
    assert phone.read_scrolls[-1] == "up"
    assert not any(call[0] in {"tap", "input", "send", "new_session", "name_search"} for call in phone.calls[before:])
    assert len([call for call in phone.calls if call[0] == "send"]) == 1
    assert list((tmp_path / "run/调试产物/douyin-direct/001").glob("submission-confirm-*/**/observation.json"))


def test_recovery_rejects_latest_mismatch_instead_of_finding_old_same_question(tmp_path):
    phone = ScrolledSubmissionPhone()
    run, store = interrupted_submission(tmp_path, phone)
    # A stale same-question screen is initially visible, but latest tail owns
    # another question. Recovery must visit latest bottom before trusting it.
    phone.position, phone.latest_question = "question", None
    original_swipe = phone.swipe
    def latest_swipe(*args, **kwargs):
        phone.latest_question = "另一个会话的问题"
        original_swipe(*args, **kwargs)
    phone.swipe = latest_swipe
    assert direct.sent(phone.observe(), run["tasks"][0])
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert phone.read_scrolls.count("up") == 1
    assert run["tasks"][0]["phase"] == "submitting"
    assert run["inflight"]["step"] == direct.ID + ".send"
    request = read_json(run["pending_request"])
    assert request["uncertain_send"] and request["allowed_resolutions"] == ["resume"]
    assert len([call for call in phone.calls if call[0] == "send"]) == 1


@pytest.mark.parametrize("fault", ["draft", "evidence", "question"])
def test_submission_recovery_refuses_unsafe_or_unproven_scene_without_scrolling(tmp_path, fault):
    phone = ScrolledSubmissionPhone()
    run, store = interrupted_submission(tmp_path, phone)
    if fault == "draft":
        phone.draft = "其他草稿"
    elif fault == "question":
        run["tasks"][0]["text"] = "断点原题被更换"
    else:
        (tmp_path / "run/调试产物/douyin-direct/001/before-send/observation.json").unlink()
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert phone.read_scrolls == []
    assert run["tasks"][0]["phase"] == "submitting"
    assert len([call for call in phone.calls if call[0] == "send"]) == 1


def test_submission_recovery_has_shared_scroll_limit(tmp_path):
    phone = ScrolledSubmissionPhone()
    run, store = interrupted_submission(tmp_path, phone)
    phone.no_boundary = True
    with pytest.raises(NeedsRepair):
        workflow.run_questions(phone, store, tmp_path / "run", run)
    assert len(phone.read_scrolls) <= 32
    assert run["tasks"][0]["phase"] == "submitting"
    assert len([call for call in phone.calls if call[0] == "send"]) == 1


@pytest.mark.parametrize("scale", [1, 1.5])
def test_sent_marker_may_disappear_after_send_with_full_evidence_chain(tmp_path, scale):
    phone = ScrolledSubmissionPhone(scale)
    run, store = interrupted_submission(tmp_path, phone)
    phone.marker = False
    workflow.run_questions(phone, store, tmp_path / "run", run)
    task = run["tasks"][0]
    assert run["status"] == "complete"
    assert direct.sent(phone.observe(), task)
    proof = direct.submission_evidence(task, tmp_path / "run")
    assert proof is not None and len(proof["end_observations"]) == 3
    assert proof["backtrack"][-1]["seam"]["verified"]
    assert len([call for call in phone.calls if call[0] == "send"]) == 1
    original = task["text"]
    task["text"] = "不同的题目"
    assert direct.submission_evidence(task, tmp_path / "run") is None
    task["text"] = original
    path = Path(proof["question_observation"]) / "screen.xml"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    assert direct.submission_evidence(task, tmp_path / "run") is None


@pytest.mark.parametrize("clipped", [False, True])
def test_latest_bubble_never_skips_different_or_partly_visible_question(clipped):
    phone = DirectPhone(stage="answer")
    phone.question = "较早的相同问题"
    observation = phone.observe()
    top, bottom = (1300, 1450) if clipped else (1000, 1100)
    canvas = Image.open(io.BytesIO(observation.png))
    ImageDraw.Draw(canvas).rectangle((200, top, 710, bottom), fill=(40, 200, 160))
    output = io.BytesIO()
    canvas.save(output, "PNG")
    observation.png = output.getvalue()
    observation.texts.append({"text": "最新的另一道问题", "confidence": .99, "bounds": (220, top + 20, 680, top + 55)})
    viewport = answer_viewport(observation)
    assert question_bubble(observation, phone.question, viewport) is not None
    nearest = latest_user_bubble(observation, viewport)
    assert nearest["text"] == "最新的另一道问题"
    if clipped:
        assert not nearest["complete"] and direct_question_bubble(observation, phone.question, viewport) is None
    else:
        with pytest.raises(CaptureError, match="第一条用户问题不匹配"):
            direct_question_bubble(observation, phone.question, viewport)


def test_foreground_miniapp_reuses_activity_without_launcher_reset_and_waits_for_results(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.platforms.douyin_direct.time.sleep", lambda _: None)

    # 1. Already inside MiniAppHostActivity0 chat: start phase skips device.launch.
    in_chat = DirectPhone(stage="chat")
    run_chat = state(direct.ID)
    workflow.run_questions(in_chat, RuleStore(tmp_path / "rules1"), tmp_path / "run1", run_chat)
    assert run_chat["status"] == "complete"
    assert ("launch", direct.PACKAGE) not in in_chat.calls

    # 2. Delayed search results after name_search wait for results_loaded before handoff/entry.
    class DelayedResultsPhone(DirectPhone):
        def __init__(self):
            super().__init__(stage="launcher")
            self.results_polls = 0

        def observe(self, ocr=False):
            obs = super().observe(ocr=ocr)
            if self.stage == "results" and self.results_polls < 2:
                self.results_polls += 1
                obs.nodes = [n for n in obs.nodes if n.get("text") not in {"综合", "视频", "小程序", "进入"}]
                obs.texts = []
            return obs

    delayed = DelayedResultsPhone()
    run_delayed = state(direct.ID)
    workflow.run_questions(delayed, RuleStore(tmp_path / "rules2"), tmp_path / "run2", run_delayed)
    assert run_delayed["status"] == "complete"
    assert delayed.results_polls == 2


def test_expanded_composer_covering_divider_scrolls_to_reveal_before_send(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "AnswerCapture", Capture)
    monkeypatch.setattr("geo_collector.recovery.time.sleep", lambda _: None)
    monkeypatch.setattr("geo_collector.platforms.douyin_direct.time.sleep", lambda _: None)

    class TallComposerPhone(DirectPhone):
        def __init__(self):
            super().__init__(stage="chat")
            self.revealed = False
            self.reveal_swipes = 0

        def observe(self, ocr=False):
            obs = super().observe(ocr=ocr)
            if self.stage != "clean":
                return obs
            # In collapsed new-session state, divider sits near the bottom of the viewport (y=1240..1280).
            # Once draft text is typed, composer expands upward (y=1200..1530) and hides the divider until scrolled!
            if not self.draft:
                for t in obs.texts:
                    if t["text"] == "已开始新会话":
                        t["bounds"] = (260, 1240, 460, 1280)
                return obs
            # Expanded composer (y=1200..1530) moves input/send upward and crops viewport bottom below 1200.
            for n in obs.nodes:
                if n.get("class_name") == "android.widget.ScrollView":
                    n["bounds"] = (0, 170, 720, 1180)
                elif n.get("class_name") == "android.widget.EditText":
                    n["bounds"] = (134, 1240, 586, 1340)
                elif n.get("text") == "发送":
                    n["bounds"] = (620, 1260, 690, 1330)
                elif n["bounds"] == (20, 1400, 700, 1530):
                    n["bounds"] = (20, 1200, 700, 1530)
            if not self.revealed:
                obs.texts = [t for t in obs.texts if t["text"] != "已开始新会话"]
            else:
                for t in obs.texts:
                    if t["text"] == "已开始新会话":
                        t["bounds"] = (260, 960, 460, 1000)
            return obs

        def swipe(self, start, end, duration, expected_package):
            assert expected_package == direct.PACKAGE
            self.calls.append(("swipe", start, end))
            if self.stage == "clean" and self.draft and end[1] < start[1]:
                self.reveal_swipes += 1
                self.revealed = True
                # Simulate losing focus on swipe to verify re-focus without overwriting draft.
                self.focused = False

    phone = TallComposerPhone()
    run = state(direct.ID)
    workflow.run_questions(phone, RuleStore(tmp_path / "rules"), tmp_path / "run", run)
    assert run["status"] == "complete"
    assert phone.reveal_swipes == 1
    assert len([c for c in phone.calls if c[0] == "send"]) == 1
    proof = direct.submission_evidence(run["tasks"][0], tmp_path / "run")
    assert proof is not None and proof["session_evidence"] == "blank" and proof["draft_evidence"] == "blank"
