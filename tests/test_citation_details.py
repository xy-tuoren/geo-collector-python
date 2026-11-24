"""douyin-direct expanded literature citation detail screenshot capture & verification."""
import io
import random
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from geo_collector.capture import (
    AnswerCapture,
    _citation_detail_valid,
    _literature_count,
    answer_viewport,
    citation_detail_rows,
    citation_state,
    verify_capture_images,
)
from geo_collector.cli import verify_run
from geo_collector.device import Observation
from geo_collector.images import CaptureError
from geo_collector.platforms import douyin_direct as direct
from geo_collector.recovery import save_observation
from geo_collector.storage import write_json


PACKAGE = direct.PACKAGE
MINIAPP_ACTIVITY = "com.tt.miniapphost.placeholder.MiniAppHostActivity0"
DETAIL_ACTIVITY = "com.ss.android.ugc.aweme.bullet.ui.BulletContainerActivity"


def _make_document(scale=1.0, height=2600):
    img = Image.new("RGB", (720, height), "white")
    draw, rng = ImageDraw.Draw(img), random.Random(901)
    for y in range(200, height - 100, 30):
        for x in range(30, 680, 22):
            draw.rectangle((x, y, x + rng.randrange(5, 16), y + rng.randrange(5, 16)), fill=(55, 55, 55))
    draw.rectangle((150, 260, 700, 350), fill=(40, 200, 160))
    if scale != 1.0:
        img = img.resize((round(720 * scale), round(height * scale)), Image.Resampling.NEAREST)
    return img


def _make_detail_png(seed, scale=1.0, blank=False):
    w, h = round(720 * scale), round(1600 * scale)
    img = Image.new("RGB", (w, h), "white")
    if not blank:
        draw, rng = ImageDraw.Draw(img), random.Random(seed)
        draw.rectangle((round(30 * scale), round(180 * scale), round(680 * scale), round(280 * scale)), fill=(30, 30, 30))
        for y in range(360, 1100, 40):
            draw.rectangle((round(40 * scale), round(y * scale), round(rng.randrange(320, 660) * scale), round((y + 18) * scale)), fill=(80, 80, 80))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeDirectCitationPhone:
    """Simulates MiniApp expanded citations + BulletContainerActivity detail pages."""

    serial = "test-direct-citations"

    def __init__(self, *, scale=1.0, header="参考2篇医学文献和2篇药品说明", items=None, fail_index=None):
        self.scale = scale
        self.header = header
        self.question = "头痛吃什么药"
        self.items = items if items is not None else [
            (1, "养血清脑颗粒联合按摩手法治疗顽固性头痛对患者...", "养血清脑颗粒联合按摩手法治疗顽固性头痛对患者血清CRP水平的影响探究", False),
            (2, "替扎尼定预防治疗慢性每日头痛的研究进展", "替扎尼定预防治疗慢性每日头痛的研究进展", False),
            (3, "对乙酰氨基酚_药品说明书", "对乙酰氨基酚药品说明书", True),
            (4, "布洛芬_药品说明书", "布洛芬药品说明书", True),
        ]
        self.fail_index = fail_index
        self.failed_once = False
        self.scroll = 0
        self.max_scroll = 500
        self.open_detail = None
        self.calls = []
        self._doc = _make_document(scale=scale)

    def _scaled(self, box):
        return tuple(round(v * self.scale) for v in box)

    def observe(self, ocr=False):
        size = (720, 1600)
        physical = self._scaled(size)
        if self.open_detail is not None:
            idx, list_title, full_title, _ = self.open_detail
            is_error = self.fail_index == idx and not self.failed_once
            png = _make_detail_png(seed=100 + idx, scale=self.scale, blank=False)
            nodes = [
                {"text": "小荷AI医生", "description": "", "resource_id": "", "bounds": (260, 90, 460, 130),
                 "class_name": "android.widget.TextView", "enabled": True, "clickable": False,
                 "focused": False, "scrollable": False, "package": PACKAGE},
            ]
            texts = [{"text": "小荷AI医生", "bounds": self._scaled((260, 90, 460, 130)), "confidence": .99}]
            if is_error:
                texts.append({"text": "内容加载失败", "bounds": self._scaled((220, 600, 500, 660)), "confidence": .99})
            else:
                nodes.extend([
                    {"text": full_title, "description": "", "resource_id": "", "bounds": (40, 200, 680, 320),
                     "class_name": "android.widget.TextView", "enabled": True, "clickable": False,
                     "focused": False, "scrollable": False, "package": PACKAGE},
                    {"text": "医学文献", "description": "", "resource_id": "", "bounds": (40, 360, 180, 400),
                     "class_name": "android.widget.TextView", "enabled": True, "clickable": False,
                     "focused": False, "scrollable": False, "package": PACKAGE},
                    {"text": "发布期刊：医疗装备", "description": "", "resource_id": "", "bounds": (40, 430, 320, 470),
                     "class_name": "android.widget.TextView", "enabled": True, "clickable": False,
                     "focused": False, "scrollable": False, "package": PACKAGE},
                    {"text": "天士力医药集团股份有限公司·上传", "description": "", "resource_id": "", "bounds": (40, 960, 520, 1000),
                     "class_name": "android.widget.TextView", "enabled": True, "clickable": False,
                     "focused": False, "scrollable": False, "package": PACKAGE},
                ])
                texts.extend([
                    {"text": full_title, "bounds": self._scaled((40, 200, 680, 320)), "confidence": .99},
                    {"text": "医学文献", "bounds": self._scaled((40, 360, 180, 400)), "confidence": .99},
                    {"text": "内容摘要：", "bounds": self._scaled((40, 520, 220, 560)), "confidence": .99},
                ])
            return Observation(png, "<hierarchy/>", PACKAGE, DETAIL_ACTIVITY, size, physical, self.serial, nodes, texts)

        # MiniApp chat answer screen
        canvas = Image.new("RGB", physical, "white")
        crop_box = self._scaled((0, 170 + self.scroll, 720, 1370 + self.scroll))
        canvas.paste(self._doc.crop(crop_box), (0, round(170 * self.scale)))
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        nodes = [
            {"text": "", "description": "", "resource_id": "", "bounds": (0, 0, 720, 1600),
             "class_name": "android.widget.FrameLayout", "enabled": True, "clickable": False,
             "focused": False, "scrollable": False, "package": PACKAGE},
            {"text": "", "description": "关闭", "resource_id": PACKAGE + ":id/close", "bounds": (636, 80, 706, 140),
             "class_name": "android.widget.ImageView", "enabled": True, "clickable": True,
             "focused": False, "scrollable": False, "package": PACKAGE},
            {"text": "", "description": "", "resource_id": "", "bounds": (0, 170, 720, 1370),
             "class_name": "android.widget.ScrollView", "enabled": True, "clickable": False,
             "focused": False, "scrollable": True, "package": PACKAGE},
            {"text": "发送消息", "description": "", "resource_id": "", "bounds": (130, 1440, 580, 1495),
             "class_name": "android.widget.EditText", "enabled": True, "clickable": True,
             "focused": False, "scrollable": False, "package": PACKAGE},
        ]
        texts = [{"text": "小荷AI医生", "bounds": self._scaled((40, 90, 260, 130)), "confidence": .99}]
        y_q = 260 - self.scroll
        if 170 <= y_q and y_q + 90 <= 1370:
            texts.append({"text": self.question, "bounds": self._scaled((180, y_q + 20, 680, y_q + 65)), "confidence": .99})
        y_h = 410 - self.scroll
        if 170 <= y_h and y_h + 45 <= 1370:
            texts.append({"text": self.header + " ^", "bounds": self._scaled((50, y_h, 540, y_h + 40)), "confidence": .99})
        y_row = 468 - self.scroll
        for idx, list_title, _, is_monograph in self.items:
            if 175 <= y_row and y_row + 36 <= 1360:
                texts.append({"text": f"{idx}. {list_title}", "bounds": self._scaled((50, y_row, 540, y_row + 34)), "confidence": .99})
                if is_monograph:
                    texts.append({"text": "药监局", "bounds": self._scaled((580, y_row, 660, y_row + 34)), "confidence": .99})
            y_row += 54
        # Body numbered list below citations (must NOT be misread as citation rows!)
        y_body = y_row + 85
        if 175 <= y_body and y_body + 36 <= 1360:
            texts.append({"text": "普通头痛常用止痛药（仅对症缓解）", "bounds": self._scaled((50, y_body, 600, y_body + 38)), "confidence": .99})
        if 175 <= y_body + 60 and y_body + 96 <= 1360:
            texts.append({"text": "1. 优先休息并监测体温变化", "bounds": self._scaled((50, y_body + 60, 560, y_body + 94)), "confidence": .99})
        return Observation(buf.getvalue(), "<hierarchy/>", PACKAGE, MINIAPP_ACTIVITY, size, physical, self.serial, nodes, texts)

    def screenshot(self):
        return self.observe().png

    def recognize(self, obs):
        return obs

    def tap(self, point, expected_package):
        assert expected_package == PACKAGE
        self.calls.append(("tap", tuple(point)))
        if self.open_detail is not None:
            return
        x, y = point
        # Map logical tap y to document y
        doc_y = y + self.scroll
        row_start = 468
        for idx, list_title, full_title, is_monograph in self.items:
            if row_start - 10 <= doc_y <= row_start + 44:
                self.open_detail = (idx, list_title, full_title, is_monograph)
                self.calls.append(("open_detail", idx, is_monograph))
                return
            row_start += 54

    def press(self, key, expected_package):
        assert key == "back" and expected_package == PACKAGE
        self.calls.append(("press", key))
        if self.open_detail is not None:
            if self.fail_index == self.open_detail[0]:
                self.failed_once = True
            self.open_detail = None

    def swipe(self, start, end, duration=.4, *, expected_package):
        assert expected_package == PACKAGE
        self.calls.append(("swipe", "down" if end[1] < start[1] else "up"))
        delta = round(start[1] - end[1])
        self.scroll = max(0, min(self.max_scroll, self.scroll + delta))


class FakeEngine:
    def __init__(self, run_dir, entry="douyin-direct"):
        self.run_dir = run_dir
        self.state = {
            "entry": entry,
            "index": 1,
            "tasks": [{"entry": entry, "index": 1, "text": "头痛吃什么药", "direct_open": {"session_confirmed": True}}],
        }
        self.events = []
        self.teach = set()

    def decorate(self, obs):
        return obs

    def layout(self, role, obs, check, request=False):
        return check(obs)

    def log(self, event, **details):
        self.events.append((event, details))

    def save(self):
        pass

    def commit_layouts(self):
        pass

    def resolve_capture_request(self, step):
        pass

    def prefer_goal(self, step, obs):
        return False

    def _request(self, step, goal, obs, hints, reason, **kw):
        raise CaptureError(f"repair:{step}:{reason}")


def _seed_direct_submission(tmp_path, phone, engine):
    debug = tmp_path / "run/调试产物/douyin-direct/001"
    debug.mkdir(parents=True, exist_ok=True)
    obs = phone.observe(ocr=True)
    q_dir = debug / "question-confirmed"
    save_observation(q_dir, obs)
    proof = {"question_observation": str(q_dir.resolve()), "identity": [phone.question, PACKAGE, direct.ID, 1]}
    engine.state["tasks"][0]["direct_submission"] = proof
    write_json(tmp_path / "run/调试产物/run.json", engine.state)
    return debug, proof


def test_literature_count_and_row_parsing_skips_monographs_and_body_list():
    assert _literature_count("参考4篇医学文献和3篇药品说明") == 4
    assert _literature_count("参考3篇药品说明书") == 0
    assert _literature_count("参考2篇医学文献、1部临床指南和2篇药品说明") == 3
    assert _literature_count("自定义参考来源") is None

    phone = FakeDirectCitationPhone(scale=1.5)
    obs = phone.observe(ocr=True)
    vp = answer_viewport(obs)
    cstate = citation_state(obs, vp)
    assert cstate["present"] and cstate["expanded"]
    parsed = citation_detail_rows(obs, vp, cstate["header"], max_total=4)
    assert [r["index"] for r in parsed["literature"]] == [1, 2]
    assert [r["index"] for r in parsed["monographs"]] == [3, 4]


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_douyin_direct_captures_only_literature_details_and_passes_verify(tmp_path, monkeypatch, scale):
    phone = FakeDirectCitationPhone(scale=scale)
    engine = FakeEngine(tmp_path / "run", entry="douyin-direct")
    debug, proof = _seed_direct_submission(tmp_path, phone, engine)
    monkeypatch.setattr(direct, "submission_evidence", lambda task, run_dir: proof)
    delivery = tmp_path / "run/交付图片/douyin-direct/001"

    capture = AnswerCapture(phone, engine, answer_page_check=direct.answer_ready, answer_page_error=direct.ANSWER_PAGE_ERROR)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    capture.settle_seconds = 2.0

    result = capture.run(phone.question, PACKAGE, delivery, debug)
    cd = result["citation_details"]
    assert cd["present"] is True and cd["complete"] is True and cd["expected_count"] == 2
    assert cd["skipped_monographs"] == 2
    assert [item["index"] for item in cd["items"]] == [1, 2]
    assert cd["items"][0]["detail_title"] == "养血清脑颗粒联合按摩手法治疗顽固性头痛对患者血清CRP水平的影响探究"
    assert [c for c in phone.calls if c[0] == "open_detail"] == [("open_detail", 1, False), ("open_detail", 2, False)]
    assert (delivery / "回答_参考文献_001.png").is_file()
    assert (delivery / "回答_参考文献_002.png").is_file()
    assert not (delivery / "回答_参考文献_003.png").exists()

    # Full offline verify_run passes
    result.update(entry="douyin-direct", index=1, question=phone.question, status="complete", outcome="captured")
    write_json(debug / "answer.json", result)
    engine.state.update(status="complete", entries=["douyin-direct"], questions=[{"index": 1, "text": phone.question}])
    engine.state["tasks"][0].update(status="complete", outcome="captured", answer=str(debug / "answer.json"))
    write_json(tmp_path / "run/调试产物/run.json", engine.state)
    verified = verify_run(tmp_path / "run")
    assert verified["complete"] is True, verified["problems"]

    # Tampering with a delivered citation detail PNG fails verify_capture_images
    (delivery / "回答_参考文献_001.png").write_bytes(_make_detail_png(999, scale=scale))
    with pytest.raises(CaptureError, match="参考文献详情交付截图与原始观察不一致"):
        verify_capture_images(result, debug, delivery)


def test_monograph_only_citations_complete_without_detail_clicks(tmp_path, monkeypatch):
    phone = FakeDirectCitationPhone(
        header="参考2篇药品说明书",
        items=[
            (1, "对乙酰氨基酚_药品说明书", "对乙酰氨基酚药品说明书", True),
            (2, "布洛芬_药品说明书", "布洛芬药品说明书", True),
        ],
    )
    engine = FakeEngine(tmp_path / "run", entry="douyin-direct")
    debug, proof = _seed_direct_submission(tmp_path, phone, engine)
    monkeypatch.setattr(direct, "submission_evidence", lambda task, run_dir: proof)
    delivery = tmp_path / "run/交付图片/douyin-direct/001"

    capture = AnswerCapture(phone, engine, answer_page_check=direct.answer_ready, answer_page_error=direct.ANSWER_PAGE_ERROR)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    result = capture.run(phone.question, PACKAGE, delivery, debug)
    assert result["citation_details"]["present"] is False
    assert result["citation_details"]["complete"] is True
    assert result["citation_details"]["skipped_monographs"] == 2
    assert not any(c[0] == "open_detail" for c in phone.calls)
    assert not list(delivery.glob("回答_参考文献_*.png"))


def test_non_direct_entry_does_not_open_citation_details(tmp_path):
    phone = FakeDirectCitationPhone()
    engine = FakeEngine(tmp_path / "run", entry="xiaohe")
    debug = tmp_path / "run/调试产物/xiaohe/001"
    delivery = tmp_path / "run/交付图片/xiaohe/001"

    capture = AnswerCapture(phone, engine, answer_page_check=lambda o: o.activity == MINIAPP_ACTIVITY)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    result = capture.run(phone.question, PACKAGE, delivery, debug)
    assert "citation_details" not in result
    assert not any(c[0] == "open_detail" for c in phone.calls)


def test_citation_detail_failure_returns_safely_and_resume_reuses_completed_items(tmp_path, monkeypatch):
    phone = FakeDirectCitationPhone(fail_index=2)
    engine = FakeEngine(tmp_path / "run", entry="douyin-direct")
    debug, proof = _seed_direct_submission(tmp_path, phone, engine)
    monkeypatch.setattr(direct, "submission_evidence", lambda task, run_dir: proof)
    delivery = tmp_path / "run/交付图片/douyin-direct/001"

    capture = AnswerCapture(phone, engine, answer_page_check=direct.answer_ready, answer_page_error=direct.ANSWER_PAGE_ERROR)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    capture.settle_seconds = 1.5

    # First attempt captures #1, hits error on #2, returns safely to MiniApp, and requests review.
    with pytest.raises(CaptureError, match="详情页加载异常"):
        capture.run(phone.question, PACKAGE, delivery, debug)
    assert phone.open_detail is None  # Safely returned to MiniApp
    assert len(engine.state["capture_preparation"]["citation_details"]["items"]) == 1

    # Resume reuses #1 without re-opening it, captures #2, and completes cleanly.
    calls_before = len(phone.calls)
    result = capture.run(phone.question, PACKAGE, delivery, debug)
    assert result["citation_details"]["complete"] is True
    reopened = [c for c in phone.calls[calls_before:] if c[0] == "open_detail"]
    assert reopened == [("open_detail", 2, False)]
    assert (delivery / "回答_参考文献_001.png").is_file()
    assert (delivery / "回答_参考文献_002.png").is_file()


def test_detail_validator_rejects_blank_and_wrong_title():
    blank_obs = Observation(_make_detail_png(1, blank=True), "<hierarchy/>", PACKAGE, DETAIL_ACTIVITY,
                            (720, 1600), (720, 1600), "dev", [], [{"text": "医学文献", "bounds": (40, 300, 180, 340), "confidence": .99}])
    assert not _citation_detail_valid(blank_obs, "养血清脑颗粒联合按摩手法", PACKAGE)

    wrong_obs = Observation(_make_detail_png(2, blank=False), "<hierarchy/>", PACKAGE, DETAIL_ACTIVITY,
                            (720, 1600), (720, 1600), "dev", [], [
                                {"text": "完全无关的糖尿病指南标题", "bounds": (40, 220, 500, 280), "confidence": .99},
                                {"text": "医学文献", "bounds": (40, 320, 180, 360), "confidence": .99},
                            ])
    assert not _citation_detail_valid(wrong_obs, "养血清脑颗粒联合按摩手法", PACKAGE)


def test_long_literature_list_scrolls_and_restores_question_top(tmp_path, monkeypatch):
    items = [
        (i, f"偏头痛临床用药疗效观察文献第{i}篇", f"偏头痛临床用药疗效观察文献第{i}篇完整标题", False)
        for i in range(1, 18)
    ]
    phone = FakeDirectCitationPhone(header="参考17篇医学文献", items=items)
    phone.max_scroll = 900
    engine = FakeEngine(tmp_path / "run", entry="douyin-direct")
    debug, proof = _seed_direct_submission(tmp_path, phone, engine)
    proof["latest_tail"] = proof["question_observation"]
    monkeypatch.setattr(direct, "submission_evidence", lambda task, run_dir: proof)
    delivery = tmp_path / "run/交付图片/douyin-direct/001"

    capture = AnswerCapture(phone, engine, answer_page_check=direct.answer_ready, answer_page_error=direct.ANSWER_PAGE_ERROR)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    capture.settle_seconds = 2.0

    result = capture.run(phone.question, PACKAGE, delivery, debug)
    cd = result["citation_details"]
    assert cd["complete"] is True and len(cd["items"]) == 17
    assert [it["index"] for it in cd["items"]] == list(range(1, 18))
    assert any(ev == "direct_submission_preparation_reused" for ev, _ in engine.events)
    assert len(list(delivery.glob("回答_参考文献_*.png"))) == 17


def test_nested_quantifier_regex_is_rejected():
    from geo_collector.rules import validate_recipe
    with pytest.raises(ValueError, match="嵌套量词"):
        validate_recipe({"actions": [{"type": "tap", "target": {"kind": "ocr", "pattern": r"(a+)+b"}}]})


def test_citation_detail_settle_ignores_status_bar_clock_tick(tmp_path, monkeypatch):
    phone = FakeDirectCitationPhone(
        header="参考1篇医学文献",
        items=[(1, "感染性腹泻诊治质量改进专家共识", "感染性腹泻诊治质量改进专家共识", False)],
    )
    ticks = [0]
    orig_screenshot = phone.screenshot

    def ticking_screenshot():
        data = orig_screenshot()
        if phone.open_detail is not None:
            ticks[0] += 1
            img = Image.open(io.BytesIO(data)).copy()
            # Mutate top status bar (y=0..40) on every screenshot probe to simulate clock/icon tick.
            ImageDraw.Draw(img).rectangle((0, 0, 120, 40), fill=(ticks[0] * 40 % 255, 0, 0))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        return data

    phone.screenshot = ticking_screenshot
    engine = FakeEngine(tmp_path / "run", entry="douyin-direct")
    debug, proof = _seed_direct_submission(tmp_path, phone, engine)
    proof["latest_tail"] = proof["question_observation"]
    monkeypatch.setattr(direct, "submission_evidence", lambda task, run_dir: proof)
    delivery = tmp_path / "run/交付图片/douyin-direct/001"

    capture = AnswerCapture(phone, engine, answer_page_check=direct.answer_ready, answer_page_error=direct.ANSWER_PAGE_ERROR)
    capture._sleep = lambda _: None
    capture.quiet_seconds = 0
    capture.settle_seconds = 2.0

    result = capture.run(phone.question, PACKAGE, delivery, debug)
    assert result["citation_details"]["complete"] is True and ticks[0] >= 1
