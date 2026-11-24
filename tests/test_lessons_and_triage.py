"""Offline tests for LessonStore casebook, pre-AI triage, and supervisor parity."""
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from geo_collector import agent_runtime, cli, device
from geo_collector.device import Observation
from geo_collector.handoff import build_handoff, handoff_summary
from geo_collector.lessons import FAILURE_LIMIT, LessonStore
from geo_collector.platforms import PACKAGES
from geo_collector.recovery import NeedsRepair, StepEngine, save_observation
from geo_collector.repair_preview import OfflineRejection
from geo_collector.rules import RuleStore
from geo_collector.storage import read_json, write_json
from geo_collector.supervisor import AgentReply, decision_plan, supervise
from geo_collector.teach import TeachSession
from geo_collector.triage import classify_failure


DOUYIN = PACKAGES["douyin"]
XIAOHE = PACKAGES["xiaohe"]


def full_decision(kind, **overrides):
    base = {
        "decision": kind,
        "candidate": None,
        "role": None,
        "direction": None,
        "distance": None,
        "detect_candidate": None,
        "interrupt_name": None,
        "point": None,
        "reason": "依据现场与历史经验做受限选择",
    }
    base.update(overrides)
    return base


def _png_with_icon(size=(360, 800), icon_center=(210, 60), color="white"):
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    cx, cy = icon_center
    draw.rounded_rectangle((cx - 14, cy - 12, cx + 14, cy + 12), radius=4, outline="black", width=3)
    draw.line((cx, cy - 6, cx, cy + 6), fill="black", width=3)
    draw.line((cx - 6, cy, cx + 6, cy), fill="black", width=3)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _paused_run(tmp_path, *, package=DOUYIN, entry="douyin-direct", activity="SearchResultActivity",
                step="douyin-direct.open_miniapp", nodes=None, texts=None, png=None, reason="找不到目标",
                allowed=None, rule_root=None):
    root = tmp_path / "batch"
    rules = rule_root or (tmp_path / "rules")
    rules.mkdir(parents=True, exist_ok=True)
    scene_dir = root / "调试产物" / "repairs" / "req1"
    obs = Observation(
        png or _png_with_icon(), "<hierarchy/>", package, activity,
        (360, 800), (360, 800), "40.4.0",
        nodes if nodes is not None else [
            {"package": package, "resource_id": f"{package}:id/retry_btn", "text": "重试",
             "description": "", "bounds": [120, 420, 240, 470], "clickable": True, "enabled": True},
        ],
        texts if texts is not None else [],
    )
    save_observation(scene_dir, obs)
    sha = hashlib.sha256(obs.png).hexdigest()
    req_file = scene_dir / "request.json"
    write_json(req_file, {
        "request_id": "req1", "status": "pending", "step": step, "entry": entry, "question_index": 1,
        "package": package, "goal": "进入小荷小程序", "reason": reason, "hints": [],
        "allowed_resolutions": allowed or ["resume", "teach.explore", "repair.perception"],
        "observation": str(scene_dir / "observation.json"), "screenshot": str(scene_dir / "screen.png"),
        "sha256": sha, "rule_assets": str(rules / "templates"), "checkpoint": {"body_frames": 0},
        "missing_evidence": [reason],
    })
    write_json(root / "调试产物" / "run.json", {
        "created_at": 100, "serial": "phone-1", "rule_root": str(rules),
        "mode": "batch", "entries": [entry], "questions": [{"index": 1, "text": "测试问题"}],
        "tasks": [{"entry": entry, "index": 1, "text": "测试问题", "phase": "open", "status": "needs_repair"}],
        "entry": entry, "index": 1, "status": "needs_repair", "pending_request": str(req_file),
    })
    return root, rules, req_file, obs


def test_lesson_store_lifecycle_strict_recall_and_decay(tmp_path):
    store = LessonStore(tmp_path / "rules")
    saved = store.add({
        "entry": "douyin-direct",
        "step": "douyin-direct.open_miniapp",
        "activity": "SearchResultActivity",
        "match_texts": ["-10009", "网络错误"],
        "strategy": "transient_retry",
        "note": "抖音搜索出现 -10009 时用 --transient 点击重试，不要记入永久路径",
    })
    assert saved["active"] and saved["package"] == DOUYIN

    # Upserting identical lesson refreshes instead of duplicating.
    again = store.add({
        "entry": "douyin-direct",
        "step": "douyin-direct.open_miniapp",
        "activity": "SearchResultActivity",
        "match_texts": ["网络错误", "-10009"],
        "strategy": "transient_retry",
        "note": "抖音搜索出现 -10009 时用 --transient 点击重试，不要记入永久路径",
    })
    assert again["id"] == saved["id"] and len(store.list(include_inactive=True)) == 1

    # Strict match_texts gating: healthy page without -10009 must not recall it.
    assert store.recall(entry="douyin-direct", package=DOUYIN, step="douyin-direct.open_miniapp",
                        activity="SearchResultActivity", texts=["小荷AI医生", "进入"]) == []
    # Error page with -10009 recalls it cleanly.
    recalled = store.recall(entry="douyin-direct", package=DOUYIN, step="douyin-direct.open_miniapp",
                            activity="SearchResultActivity", texts=["网络错误 -10009", "重试"])
    assert len(recalled) == 1 and recalled[0]["id"] == saved["id"] and recalled[0]["strategy"] == "transient_retry"
    assert "-10009" in recalled[0]["matched"]

    # Wrong entry never recalls a douyin-direct scoped lesson.
    assert store.recall(entry="xiaohe", package=XIAOHE, step="douyin-direct.open_miniapp",
                        texts=["-10009"]) == []

    # Three consecutive failures auto-disable; success resets failures.
    for _ in range(FAILURE_LIMIT - 1):
        store.record_outcome(saved["id"], success=False, reason="未生效")
    assert store.list()
    store.record_outcome(saved["id"], success=True)
    assert store.list()[0]["failures"] == 0 and store.list()[0]["successes"] == 1
    for _ in range(FAILURE_LIMIT):
        store.record_outcome(saved["id"], success=False, reason="改版失效")
    assert store.list() == [] and len(store.list(include_inactive=True)) == 1


def test_offline_teach_lesson_staging_promotion_and_cli_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "Device", lambda **kw: pytest.fail("lessons and teach lesson must be offline"))
    root, rules, _, _ = _paused_run(
        tmp_path,
        texts=[{"text": "网络错误 -10009", "confidence": .99, "bounds": [40, 300, 320, 340]}],
    )

    # 1. Stage lesson bound to the paused run (not active in lessons.json yet).
    staged = cli.execute(cli.parser().parse_args([
        "teach", str(root), "lesson",
        "--strategy", "transient_retry",
        "--match", "-10009",
        "--note", "遇到 -10009 直接 --transient 点重试",
    ]))
    assert staged["staged"] is True and staged["promoted"] is False
    assert LessonStore(rules).list() == []

    # 2. Promoting flows after verified question completion promotes the staged lesson.
    state = read_json(root / "调试产物" / "run.json")
    engine = StepEngine(None, RuleStore(rules), root, state)
    engine.commit_flows()
    promoted = LessonStore(rules).list()
    assert len(promoted) == 1 and promoted[0]["step"] == "douyin-direct.open_miniapp"
    assert promoted[0]["activity"] == "SearchResultActivity"

    # 3. Handoff recalls the verified lesson automatically without leaking rule_root.
    summary = handoff_summary(root)
    assert summary["prior_lessons"][0]["id"] == promoted[0]["id"]
    assert str(rules) not in json.dumps(summary, ensure_ascii=False)

    # 4. Offline CLI `lessons` commands work end-to-end.
    listed = cli.execute(cli.parser().parse_args(["lessons", "--rule-dir", str(rules)]))
    assert len(listed["lessons"]) == 1
    cli.execute(cli.parser().parse_args(["lessons", "disable", promoted[0]["id"], "--rule-dir", str(rules), "--reason", "测试停用"]))
    assert cli.execute(cli.parser().parse_args(["lessons", "list", "--rule-dir", str(rules)]))["lessons"] == []
    cli.execute(cli.parser().parse_args(["lessons", "enable", promoted[0]["id"], "--rule-dir", str(rules)]))
    fb = cli.execute(cli.parser().parse_args(["lessons", "feedback", promoted[0]["id"], "--outcome", "success", "--rule-dir", str(rules)]))
    assert fb["lesson"]["successes"] == 1


def test_triage_separates_auth_transient_detector_and_ignores_medical_prose():
    # Auth / captcha activity skips AI.
    auth = classify_failure(
        {"step": "douyin.open_search", "package": DOUYIN},
        {"package": DOUYIN, "activity": "com.ss.android.ugc.aweme.account.LoginActivity", "nodes": [], "texts": []},
    )
    assert auth["failure_class"] == "user_action_required" and auth["skip_ai"] is True

    # Captcha UI label skips AI.
    captcha = classify_failure(
        {"step": "douyin.open_search", "package": DOUYIN},
        {"package": DOUYIN, "activity": "MainActivity", "nodes": [],
         "texts": [{"text": "请完成安全验证", "confidence": .98, "bounds": [40, 200, 300, 250]}]},
    )
    assert captcha["failure_class"] == "user_action_required" and captcha["skip_ai"] is True

    # Long medical prose containing words like "验证" or "错误" must NOT trigger auth/network false positives.
    prose = classify_failure(
        {"step": "citation.expand", "package": DOUYIN},
        {"package": DOUYIN, "activity": "MiniAppHostActivity0", "nodes": [],
         "texts": [{"text": "临床验证表明，如果长期用药错误可能会导致轻度脱水或电解质失衡，需要及时补充口服补液盐。",
                    "confidence": .99, "bounds": [20, 400, 340, 500]}]},
    )
    assert prose["failure_class"] == "ui_drift" and prose["skip_ai"] is False

    # Network error -10009 + 重试 button classified as transient_env with auto_retry_candidate.
    net = classify_failure(
        {"step": "douyin-direct.open_miniapp", "package": DOUYIN},
        {"package": DOUYIN, "activity": "SearchResultActivity", "nodes": [],
         "texts": [{"text": "网络错误 -10009", "confidence": .99, "bounds": [40, 300, 300, 340]},
                   {"text": "重试", "confidence": .99, "bounds": [120, 420, 240, 470]}]},
        candidates=[{"id": "o1", "label": "网络错误 -10009"}, {"id": "o2", "label": "重试"}],
    )
    assert net["failure_class"] == "transient_env" and net["auto_retry_candidate"] == "o2"

    # Capture seam review and unloaded product images classified as detector_gap.
    gap = classify_failure(
        {"step": "capture.review", "package": DOUYIN, "reason": "最后两帧接缝未通过原图像素检查"},
        {"package": DOUYIN, "activity": "MiniAppHostActivity0", "nodes": [], "texts": []},
    )
    assert gap["failure_class"] == "detector_gap" and gap["skip_ai"] is False
    img_wait = classify_failure(
        {"step": "products.images_wait", "package": DOUYIN, "reason": "药品图片未加载，等待超时"},
        {"package": DOUYIN, "activity": "MiniAppHostActivity0", "nodes": [], "texts": []},
    )
    assert img_wait["failure_class"] == "detector_gap" and img_wait["skip_ai"] is False


def test_supervisor_skips_ai_on_auth_triage_and_credits_recalled_lessons(tmp_path):
    # 1. Auth screen: supervise stops immediately with 0 AI calls.
    auth_root, _, auth_req, _ = _paused_run(
        tmp_path / "auth",
        activity="com.ss.android.ugc.aweme.account.LoginActivity",
        texts=[{"text": "获取验证码", "confidence": .99, "bounds": [60, 300, 240, 350]}],
    )
    def forbidden(*args, **kwargs):
        pytest.fail("auth/captcha screen must never invoke AI")
    stopped = supervise(
        auth_root,
        resume=lambda: (_ for _ in ()).throw(NeedsRepair(auth_req)),
        inspect=lambda: {"scene": str(auth_req.parent)},
        apply=forbidden,
        agent=forbidden,
    )
    assert stopped["status"] == "needs_repair" and stopped["supervisor"]["calls"] == 0
    assert "登录" in stopped["stop_reason"]

    # 2. Normal screen with matching prior_lesson: lesson is passed to agent and credited once on completion.
    root, rules, req_file, _ = _paused_run(
        tmp_path / "credit",
        texts=[{"text": "网络错误 -10009", "confidence": .99, "bounds": [40, 300, 320, 340]}],
    )
    lesson = LessonStore(rules).add({
        "entry": "douyin-direct",
        "step": "douyin-direct.open_miniapp",
        "match_texts": ["-10009"],
        "strategy": "transient_retry",
        "note": "直接 transient_tap 重试",
    })
    done = False
    seen_lessons = []

    def resume():
        if not done:
            raise NeedsRepair(req_file)
        return {"complete": True}

    def agent(context, *, include_image):
        seen_lessons.extend(context["prompt"]["prior_lessons"])
        return AgentReply(full_decision("transient_tap", candidate="n1"))

    def apply(decision, context):
        nonlocal done
        assert context["plan"]["transient"] is True
        done = True

    result = supervise(root, resume=resume, inspect=lambda: {"scene": str(req_file.parent)}, apply=apply, agent=agent)
    assert result["complete"] is True and seen_lessons[0]["id"] == lesson["id"]
    assert LessonStore(rules).list()[0]["successes"] == 1


def test_agent_runtime_executes_transient_interrupt_and_point_template(tmp_path, monkeypatch):
    monkeypatch.setattr("geo_collector.teach.time.sleep", lambda _: None)

    class Phone:
        def __init__(self, first, second=None):
            self.current, self.second = first, second
            self.taps = []

        def observe(self, **_):
            return self.current

        def tap(self, point, *, expected_package):
            assert expected_package == self.current.package
            self.taps.append(tuple(point))
            if self.second is not None:
                self.current = self.second

    # A. transient_tap executes without recording into exploration.route and records recalled lesson for credit.
    root_t, rules_t, _, obs_t = _paused_run(tmp_path / "transient")
    tracked = LessonStore(rules_t).add({
        "entry": "douyin-direct", "step": "douyin-direct.open_miniapp",
        "match_texts": ["重试"], "strategy": "transient_retry", "note": "用 transient 点重试",
    })
    phone_t = Phone(obs_t, replace(obs_t, nodes=[]))
    session_t = TeachSession(phone_t, RuleStore(rules_t), root_t, read_json(root_t / "调试产物" / "run.json"))
    monkeypatch.setattr(session_t.engine, "observe", phone_t.observe)
    ctx_t = build_handoff(root_t)
    res_t = agent_runtime.apply_decision(session_t, full_decision("transient_tap", candidate="n1"), ctx_t)
    assert res_t["transient"] is True and res_t["recorded"] is False and res_t["route_steps"] == 0
    assert phone_t.taps == [(180.0, 445.0)]
    assert tracked["id"] in session_t.state.get("recalled_lessons", [])
    session_t.engine.commit_flows()
    assert LessonStore(rules_t).list()[0]["successes"] == 1

    # B. interrupt validates popup presence, dismisses, and promotes interrupt.* into RuleStore.
    popup_nodes = [
        {"package": DOUYIN, "resource_id": f"{DOUYIN}:id/popup_title", "text": "发现新版本",
         "description": "", "bounds": [60, 260, 300, 310], "clickable": False, "enabled": True},
        {"package": DOUYIN, "resource_id": f"{DOUYIN}:id/popup_close", "text": "关闭",
         "description": "关闭", "bounds": [140, 420, 220, 470], "clickable": True, "enabled": True},
    ]
    root_i, rules_i, _, obs_i = _paused_run(tmp_path / "interrupt", nodes=popup_nodes)
    phone_i = Phone(obs_i, replace(obs_i, nodes=[]))
    session_i = TeachSession(phone_i, RuleStore(rules_i), root_i, read_json(root_i / "调试产物" / "run.json"))
    monkeypatch.setattr(session_i.engine, "observe", phone_i.observe)
    ctx_i = build_handoff(root_i)
    ids = {row.get("label"): row["id"] for row in ctx_i["prompt"]["candidates"]}
    res_i = agent_runtime.apply_decision(
        session_i,
        full_decision("interrupt", detect_candidate=ids["发现新版本"], interrupt_name="update_popup", candidate=ids["关闭"]),
        ctx_i,
    )
    assert res_i["promoted"] is True and res_i["step"] == "interrupt.update_popup"
    assert RuleStore(rules_i).get(DOUYIN, "interrupt.update_popup") is not None

    # C. point_template requires image_included=True, rejects blank pixels offline, and extracts template on valid icon.
    root_p, rules_p, _, obs_p = _paused_run(tmp_path / "point", nodes=[
        {"package": DOUYIN, "resource_id": f"{DOUYIN}:id/sgk", "text": "", "description": "",
         "bounds": [0, 0, 360, 120], "clickable": False, "enabled": True},
    ], png=_png_with_icon(size=(360, 800), icon_center=(210, 60)))
    phone_p = Phone(obs_p, replace(obs_p, nodes=[]))
    session_p = TeachSession(phone_p, RuleStore(rules_p), root_p, read_json(root_p / "调试产物" / "run.json"))
    monkeypatch.setattr(session_p.engine, "observe", phone_p.observe)
    ctx_p = build_handoff(root_p)
    with pytest.raises(ValueError, match="need_image"):
        decision_plan(full_decision("point_template", point=[210 / 360, 60 / 800]), ctx_p)
    ctx_with_img = {**ctx_p, "image_included": True}
    with pytest.raises(OfflineRejection, match="视觉特征"):
        agent_runtime.apply_decision(session_p, full_decision("point_template", point=[0.5, 0.5]), ctx_with_img)
    assert not (rules_p / "templates").exists()
    res_p = agent_runtime.apply_decision(session_p, full_decision("point_template", point=[210 / 360, 60 / 800]), ctx_with_img)
    assert res_p["recorded"] is True and res_p["action"]["target"]["kind"] == "template"
    assert len(list((rules_p / "templates").glob("*.png"))) == 1
    assert phone_p.taps == [(pytest.approx(210.0, abs=2), pytest.approx(60.0, abs=2))]
