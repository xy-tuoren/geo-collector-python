"""No phone or model calls: event-driven handoff and budget regression tests."""
import hashlib
import json
from pathlib import Path
import sys
import tomllib

import pytest

from geo_collector.handoff import build_handoff, handoff_summary
from geo_collector.recovery import NeedsRepair
from geo_collector.storage import read_json, write_json
from geo_collector.supervisor import (AgentReply, CodexAgent, SupervisorLimits, _cli_failure, _isolated_codex_home,
                                      _usage, decision_plan, supervise, usage_summary)


def choice(kind="tap", **values):
    return {"decision": kind, "candidate": "n1" if kind == "tap" else None, "role": None,
            "direction": None, "distance": None, "reason": "可见控件对应当前目标", **values}


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "batch"
    scene = root / "调试产物" / "repairs" / "request1"
    scene.mkdir(parents=True)
    (scene / "screen.png").write_bytes(b"test png evidence")
    write_json(scene / "observation.json", {"xml": "private full hierarchy", "package": "app.demo",
               "activity": "Page", "version": "1", "logical_size": [1080, 2400], "physical_size": [1080, 2400],
               "nodes": [{"resource_id": "app.demo:id/close", "text": "关闭", "bounds": [100, 200, 300, 400],
                          "clickable": True, "class_name": "Button", "package": "app.demo"}], "texts": []})
    request = {"status": "pending", "step": "overlay.dismiss", "objective": {"description": "关闭遮挡弹窗"},
               "allowed_resolutions": ["resume", "teach.explore", "repair.perception"],
               "observation": str(scene / "observation.json"), "screenshot": str(scene / "screen.png"),
               "sha256": hashlib.sha256((scene / "screen.png").read_bytes()).hexdigest(),
               "checkpoint": {"body_frames": 0}, "missing_evidence": ["弹层遮挡"]}
    write_json(scene / "request.json", request)
    write_json(root / "调试产物" / "run.json", {"created_at": 42, "serial": "private-device", "rule_root": "private-rules",
               "mode": "batch", "entries": ["xiaohe"], "questions": [{"index": 1, "text": "示例问题"}],
               "tasks": [{"entry": "xiaohe", "index": 1, "text": "示例问题", "phase": "capture", "status": "needs_repair"}],
               "entry": "xiaohe", "index": 1, "status": "needs_repair", "pending_request": str(scene / "request.json")})
    return root


def request_for(run):
    return Path(read_json(run / "调试产物" / "run.json")["pending_request"])


def pending(run):
    raise NeedsRepair(request_for(run))


def status_for(run, **extra):
    return {"scene": str(request_for(run).parent), **extra}


def test_handoff_is_bounded_and_excludes_run_history_device_and_xml(run):
    file = request_for(run).parent / "observation.json"
    value = read_json(file)
    value["nodes"] *= 100
    value["texts"] = [{"text": "正文" * 1000, "confidence": .99, "bounds": [0, 100, 1000, 180]}] * 100
    write_json(file, value)
    context = build_handoff(run, limit=5)
    text = json.dumps(context["prompt"], ensure_ascii=False)
    assert "private-device" not in text and "private-rules" not in text and "private full hierarchy" not in text
    assert len(context["prompt"]["candidates"]) == 13
    assert context["prompt"]["candidates_truncated"] is True
    assert len(text) < 6000


def test_handoff_uses_latest_teach_scene_without_another_observation(run):
    scene = run / "调试产物" / "teach" / "latest"
    scene.mkdir(parents=True)
    (scene / "screen.png").write_bytes(b"latest")
    write_json(scene / "observation.json", {"package": "app.demo", "activity": "After", "logical_size": [100, 200],
                                            "physical_size": [100, 200], "nodes": [], "texts": []})
    context = build_handoff(run, {"scene": str(scene), "goal_check": True})
    assert context["prompt"]["page"]["activity"] == "After"
    assert context["sha256"] == hashlib.sha256(b"latest").hexdigest()


def test_handoff_rejects_evidence_outside_original_run(run, tmp_path):
    with pytest.raises(ValueError, match="原批次"):
        build_handoff(run, {"scene": str(tmp_path)})


def test_same_failure_ignores_clock_animation_and_generated_text(run):
    file = request_for(run).parent / "observation.json"
    original = read_json(file)
    original["nodes"].append({"text": "先生成的回答", "resource_id": "app.demo:id/answer", "bounds": [20, 600, 1000, 1200]})
    original["texts"] = [{"text": "12:34", "confidence": .99, "bounds": [20, 10, 160, 60]},
                         {"text": "先生成的回答", "confidence": .99, "bounds": [20, 600, 1000, 650]}]
    write_json(file, original)
    first = build_handoff(run)
    original["texts"][0]["text"] = "12:35"
    original["texts"][1]["text"] = "随后生成的不同回答和视频字幕"
    original["nodes"][-1]["text"] = "随后生成的不同回答"
    original["nodes"][-1]["bounds"][3] = 1800
    write_json(file, original)
    (file.parent / "screen.png").write_bytes(b"different animation pixels")
    second = build_handoff(run)
    assert first["sha256"] != second["sha256"]
    assert first["fingerprint"] == second["fingerprint"]
    original["nodes"][0]["text"] = "查看全文"
    write_json(file, original)
    assert first["fingerprint"] != build_handoff(run)["fingerprint"]


def test_checkpoint_progress_permits_a_new_handoff(run):
    first = build_handoff(run)
    request = read_json(request_for(run))
    request["checkpoint"]["body_frames"] = 2
    write_json(request_for(run), request)
    assert first["fingerprint"] != build_handoff(run)["fingerprint"]


def test_goal_search_control_at_end_of_large_tree_survives_limit_and_deduplicates(run):
    file = request_for(run).parent / "observation.json"
    value = read_json(file)
    value["nodes"] = [{"resource_id": f"app.demo:id/feed{i}", "description": "点赞评论",
                       "clickable": True, "bounds": [900, 500 + i * 20, 1060, 510 + i * 20],
                       "class_name": "LinearLayout"} for i in range(60)]
    value["nodes"] += [{"description": "搜索", "clickable": True, "bounds": [900, 100, 1080, 220], "class_name": "Button"},
                       {"resource_id": "app.demo:id/search", "description": "搜索", "clickable": False,
                        "bounds": [930, 120, 1050, 200], "class_name": "Button"}]
    write_json(file, value)
    request = read_json(request_for(run))
    request.update(goal="打开搜索", hints=["搜索"])
    write_json(request_for(run), request)
    context = build_handoff(run, limit=5)
    assert context["prompt"]["candidates"][0]["label"] == "搜索"
    assert context["targets"]["n1"]["value"] == "app.demo:id/search"
    assert sum(item.get("label") == "搜索" for item in context["prompt"]["candidates"]) == 1
    assert len(context["prompt"]["candidates"]) == 5


def test_completed_batch_has_no_pending_handoff(run):
    file = run / "调试产物" / "run.json"
    state = read_json(file)
    state.update(status="complete")
    state.pop("pending_request")
    write_json(file, state)
    result = handoff_summary(run)
    assert result["status"] == "complete" and result["pending"] is False and result["request"] is None


def test_normal_local_completion_never_inspects_or_calls_ai(run):
    def forbidden(*args, **kwargs):
        pytest.fail("normal local completion cannot invoke AI or inspect")
    result = supervise(run, resume=lambda: {"complete": True}, inspect=forbidden, apply=forbidden, agent=forbidden)
    assert result["complete"] is True
    assert result["supervisor"]["calls"] == 0
    assert result["supervisor"]["input_tokens"] == 0


def test_incomplete_collector_reports_saved_failure_without_guessing_or_leaking_errors(run):
    state_file = run / "调试产物" / "run.json"
    state = read_json(state_file)
    state["tasks"][0].update(status="failed", phase="open")
    state.pop("pending_request")
    write_json(state_file, state)
    write_json(run / "调试产物" / "xiaohe" / "001" / "failure.json",
               {"type": "RuntimeError", "error": "前台应用不符 Authorization: private-secret"})
    def forbidden(*args, **kwargs):
        pytest.fail("an unstructured collector error cannot invoke AI or phone actions")
    result = supervise(run, resume=lambda: {"complete": False, "problems": ["xiaohe/001 未完成"]},
                       inspect=forbidden, apply=forbidden, agent=forbidden)
    assert result["status"] == "incomplete" and result["supervisor"]["calls"] == 0
    assert result["collector_failure"]["phase"] == "open"
    assert result["collector_failure"]["reason"] == "前台应用或页面不符合当前步骤要求"
    assert "private-secret" not in json.dumps(result) and "受限接管请求" in result["stop_reason"]
    assert read_json(run / "调试产物" / "supervisor.json")["collector_failure"] == result["collector_failure"]


def test_unstructured_collector_exception_is_failed_without_a_fabricated_handoff(run):
    def resume():
        raise RuntimeError("unexpected failure with private-secret")
    def forbidden(*args, **kwargs):
        pytest.fail("runtime exceptions cannot invoke AI or inspect")
    result = supervise(run, resume=resume, inspect=forbidden, apply=forbidden, agent=forbidden)
    assert result["status"] == "failed" and result["supervisor"]["calls"] == 0
    assert "request" not in result and "private-secret" not in json.dumps(result)


def test_successful_supervisor_resume_clears_old_stop_reason(run):
    def forbidden(*args, **kwargs):
        pytest.fail("normal completion cannot call AI")
    supervise(run, resume=lambda: {"complete": False}, inspect=forbidden, apply=forbidden, agent=forbidden)
    result = supervise(run, resume=lambda: {"complete": True}, inspect=forbidden, apply=forbidden, agent=forbidden)
    assert result["status"] == "complete" and "stop_reason" not in result
    assert "stop_reason" not in read_json(run / "调试产物" / "supervisor.json")


def test_one_unknown_scene_calls_once_then_continues_whole_batch(run):
    done = False
    calls = []
    def resume():
        if not done:
            pending(run)
        return {"complete": True, "captured": 15}
    def apply(decision, context):
        nonlocal done
        assert context["plan"]["actions"][0]["target"]["value"] == "app.demo:id/close"
        done = True
    def agent(context, *, include_image):
        calls.append(include_image)
        return AgentReply(choice(), {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 0})
    result = supervise(run, resume=resume, inspect=lambda: status_for(run), apply=apply, agent=agent)
    assert result["captured"] == 15 and calls == [False]
    assert result["supervisor"]["input_tokens"] == 20


def test_unchanged_failure_is_deduplicated_across_supervisor_restarts(run):
    calls = []
    def agent(context, **kwargs):
        calls.append(context["fingerprint"])
        return AgentReply(choice("resume"))
    kwargs = dict(resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    result = supervise(run, **kwargs)
    assert result["status"] == "needs_repair" and "相同故障" in result["stop_reason"]
    result2 = supervise(run, **kwargs)
    assert result2["status"] == "needs_repair" and len(calls) == 1


def test_uncertain_send_never_invokes_ai_or_action(run):
    file = request_for(run)
    request = read_json(file)
    request.update(uncertain_send=True, allowed_resolutions=["resume"])
    write_json(file, request)
    resumes = []
    def resume():
        resumes.append(1)
        pending(run)
    def forbidden(*args, **kwargs):
        pytest.fail("uncertain sends permit only local read-only resume")
    result = supervise(run, resume=resume, inspect=forbidden, apply=forbidden, agent=forbidden)
    assert len(resumes) == 3
    assert result["supervisor"]["calls"] == 0 and "只读确认" in result["stop_reason"]


def test_image_only_attached_after_explicit_request_and_budget_includes_both(run):
    calls = []
    def agent(context, *, include_image):
        calls.append(include_image)
        return AgentReply(choice("need_image" if not include_image else "stop"))
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    assert calls == [False, True]
    assert result["supervisor"]["calls"] == 2 and result["supervisor"]["image_calls"] == 1
    assert result["supervisor"]["input_tokens"] is None


@pytest.mark.parametrize("limits, expected", [(SupervisorLimits(max_calls=1), "批次"),
                                               (SupervisorLimits(max_calls_per_task=1), "本题")])
def test_budget_prevents_second_image_call(run, limits, expected):
    calls = []
    def agent(*args, **kwargs):
        calls.append(1)
        return AgentReply(choice("need_image"))
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None,
                       agent=agent, limits=limits)
    assert len(calls) == 1 and expected in result["stop_reason"]


def test_failure_reserved_before_dispatch_and_usage_is_not_fabricated(run):
    def agent(*args, **kwargs):
        assert read_json(run / "调试产物" / "supervisor.json")["calls"][0]["status"] == "started"
        raise TimeoutError()
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    assert result["supervisor"]["calls"] == 1 and result["supervisor"]["input_tokens"] is None
    assert read_json(run / "调试产物" / "supervisor.json")["calls"][0]["error_type"] == "TimeoutError"


def test_explicit_retry_only_retries_previously_failed_inference_once(run):
    attempts = []
    def agent(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError()
    kwargs = dict(resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    supervise(run, **kwargs)
    assert len(attempts) == 1
    supervise(run, **kwargs)
    assert len(attempts) == 1
    result = supervise(run, **kwargs, retry_failed=True)
    assert len(attempts) == 2 and result["supervisor"]["calls"] == 2
    assert "超时" in result["stop_reason"]


@pytest.mark.parametrize("status", ["started", "returned"])
def test_retry_error_cannot_retry_uncertain_or_returned_suggestion(run, status):
    calls = []
    def agent(*args, **kwargs):
        calls.append(1)
        return AgentReply(choice("resume"))
    kwargs = dict(resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    supervise(run, **kwargs)
    file = run / "调试产物" / "supervisor.json"
    ledger = read_json(file)
    ledger["calls"][0]["status"] = status
    write_json(file, ledger)
    result = supervise(run, **kwargs, retry_failed=True)
    assert len(calls) == 1 and "相同故障" in result["stop_reason"]


def test_explicit_failed_retry_still_obeys_original_budget(run):
    def agent(*args, **kwargs):
        raise TimeoutError()
    kwargs = dict(resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None,
                  agent=agent, limits=SupervisorLimits(max_calls=1))
    supervise(run, **kwargs)
    result = supervise(run, **kwargs, retry_failed=True)
    assert result["supervisor"]["calls"] == 1 and "上限" in result["stop_reason"]


def test_explicit_failed_image_retry_skips_duplicate_text_inference(run):
    calls = []
    def agent(context, *, include_image):
        calls.append(include_image)
        if not include_image:
            return AgentReply(choice("need_image"))
        raise TimeoutError()
    kwargs = dict(resume=lambda: pending(run), inspect=lambda: status_for(run), apply=lambda *_: None, agent=agent)
    supervise(run, **kwargs)
    supervise(run, **kwargs, retry_failed=True)
    assert calls == [False, True, True]


def test_binding_prevents_reuse_for_other_device(run):
    supervise(run, resume=lambda: {"complete": True}, inspect=lambda: None, apply=lambda *_: None)
    file = run / "调试产物" / "run.json"
    state = read_json(file)
    state["serial"] = "another-phone"
    write_json(file, state)
    with pytest.raises(ValueError, match="原批次"):
        supervise(run, resume=lambda: {"complete": True}, inspect=lambda: None, apply=lambda *_: None)


def test_best_effort_goal_does_not_create_unbounded_local_loop(run):
    calls = []
    def agent(*args, **kwargs):
        calls.append(1)
        return AgentReply(choice("stop"))
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run, goal_check=True),
                       apply=lambda *_: None, agent=agent)
    assert len(calls) == 1 and result["status"] == "needs_repair"


@pytest.mark.parametrize("decision", [choice(candidate="made-up"), choice("tap", distance=.5),
                                       choice("perception", candidate="n1", role="complete"),
                                       choice("swipe", direction="up", distance=10),
                                       {**choice(), "shell": "adb tap"}])
def test_invalid_or_unobserved_model_actions_rejected_before_dispatch(run, decision):
    with pytest.raises(ValueError):
        decision_plan(decision, build_handoff(run))


def test_readonly_or_uncertain_context_cannot_be_overridden_by_model(run):
    context = build_handoff(run)
    context["prompt"]["readonly"] = True
    with pytest.raises(ValueError, match="不允许导航"):
        decision_plan(choice(), context)
    context["prompt"]["uncertain"] = True
    with pytest.raises(ValueError, match="只读"):
        decision_plan(choice("perception", candidate="n1", role="question"), context)


def test_swipe_preserves_the_validated_relative_distance_contract(run):
    plan = decision_plan(choice("swipe", direction="up", distance=.65), build_handoff(run))
    assert plan == {"actions": [{"type": "swipe", "direction": "up", "distance_fraction": .65}], "explore": True}


@pytest.mark.parametrize("label", ["查看全部药品", "全部药品 >", "更多药品", "查看所有推荐药品"])
@pytest.mark.parametrize("source", ["node", "ocr"])
def test_products_entry_role_requires_a_real_list_entry_label(run, label, source):
    file = request_for(run).parent / "observation.json"
    value = read_json(file)
    if source == "node":
        value["nodes"][0]["text"] = label
    else:
        value["nodes"] = []
        value["texts"] = [{"text": label, "confidence": .99, "bounds": [100, 200, 300, 400]}]
    write_json(file, value)
    context = build_handoff(run)
    candidate = context["prompt"]["candidates"][0]["id"]
    plan = decision_plan(choice("perception", candidate=candidate, role="products_entry"), context)
    assert plan["perception"]["products_entry"] == context["targets"][candidate]


@pytest.mark.parametrize("label", ["思密达·蒙脱石散", "去购买", "药物指南", "查看说明书", "参考药品", "", "查看全部药品 思密达"])
def test_products_entry_role_rejects_medicine_cards_and_unrelated_controls(run, label):
    file = request_for(run).parent / "observation.json"
    value = read_json(file)
    value["nodes"][0]["text"] = label
    write_json(file, value)
    context = build_handoff(run)
    with pytest.raises(ValueError, match="只能绑定可见的全部或更多药品列表入口"):
        decision_plan(choice("perception", candidate="n1", role="products_entry"), context)


def test_incorrect_medicine_entry_suggestion_stops_before_binding_or_applying(run):
    file = request_for(run).parent / "observation.json"
    value = read_json(file)
    value["nodes"][0]["text"] = "思密达·蒙脱石散"
    write_json(file, value)
    def apply(*args, **kwargs):
        pytest.fail("a medicine card must never become a synthetic all-products entry")
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run), apply=apply,
                       agent=lambda *args, **kwargs: AgentReply(choice("perception", candidate="n1", role="products_entry"),
                                                               {"input_tokens": 100, "output_tokens": 10}))
    assert result["status"] == "needs_repair" and result["supervisor"]["calls"] == 1
    assert result["supervisor"]["input_tokens"] == 100
    assert "不能以药品名" in result["stop_reason"]
    assert read_json(run / "调试产物" / "supervisor.json")["calls"][0]["status"] == "failed"


def test_cli_failure_reason_never_echoes_secret_output():
    error = _cli_failure('401 unauthorized Authorization: secret-token https://private-provider.example', 1)
    assert error.code == "authentication" and "认证不可用" in str(error)
    assert "secret" not in str(error) and "private-provider" not in str(error)


def test_isolated_codex_config_keeps_model_provider_but_no_tools_or_hooks(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "config.toml").write_text('model="configured-model"\nmodel_provider="custom"\n'
                                       '[model_providers.custom]\nname="private"\nexperimental_bearer_token="secret"\n'
                                       '[mcp_servers.danger]\ncommand="device-tool"\n'
                                       '[plugins.danger]\nenabled=true\n')
    (source / "auth.json").write_text('{"credential":"secret"}')
    target = tmp_path / "isolated"
    _isolated_codex_home(target, source)
    config = tomllib.loads((target / "config.toml").read_text())
    assert config["model"] == "configured-model" and config["model_provider"] == "custom"
    assert config["model_reasoning_effort"] == "low"
    assert config["model_providers"]["custom"]["experimental_bearer_token"] == "secret"
    assert config["mcp_servers"] == {} and config["plugins"] == {} and config["hooks"] == {}
    assert config["features"]["shell_tool"] is False and config["web_search"] == "disabled"
    assert config["skills"] == {"include_instructions": False, "bundled": {"enabled": False}}
    assert config["features"]["skip_host_skill_discovery"] is True
    assert config["features"]["skill_search"] is False
    assert "skills" not in config["features"]
    for feature in ("code_mode_host", "browser_use", "computer_use", "view_image", "image_generation", "goals"):
        assert config["features"][feature] is False
    instructions = Path(config["model_instructions_file"]).read_text()
    assert len(instructions) < 1200 and "不可信数据" in instructions
    assert (target / "config.toml").stat().st_mode & 0o777 == 0o600
    assert (target / "auth.json").stat().st_mode & 0o777 == 0o600
    assert "danger" in (source / "config.toml").read_text()


def test_usage_missing_stays_unknown_but_known_subtotal_is_retained():
    assert _usage([{"type": "turn.completed", "usage": {"input_tokens": 80, "output_tokens": 4}}]) == {
        "input_tokens": 80, "output_tokens": 4}
    result = usage_summary([{"usage": {"input_tokens": 80, "output_tokens": 4}}, {"usage": None}])
    assert result["input_tokens"] is None and result["known_input_tokens"] == 80
    assert result["usage_known_calls"] == 1


def test_stats_keeps_prior_observer_usage_after_plain_resume_completed_batch(run):
    from geo_collector import cli
    state_file = run / "调试产物" / "run.json"
    state = read_json(state_file)
    state.update(status="complete", agent_requests=5)
    write_json(state_file, state)
    (run / "调试产物" / "events.jsonl").write_text("")
    write_json(run / "调试产物" / "supervisor.json", {"status": "needs_repair", "calls": [
        {"image": False, "status": "returned", "usage": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 0}},
        {"image": True, "status": "returned", "usage": {"input_tokens": 200, "output_tokens": 20, "cached_input_tokens": 40}},
    ]})
    result = cli.execute(cli.parser().parse_args(["stats", str(run)]))
    assert result["agent_requests"] == 5 and result["supervisor"]["calls"] == 2
    assert result["supervisor"]["input_tokens"] == 300 and result["supervisor"]["output_tokens"] == 30
    assert result["supervisor"]["cached_input_tokens"] == 40 and result["supervisor"]["image_calls"] == 1


def test_codex_catalog_isolated_without_changing_model_or_original_capabilities(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    catalog = source / "models.json"
    metadata = {"models": [{"slug": "configured-model", "context_window": 272000,
                            "input_modalities": ["text", "image"], "tool_mode": "code_mode_only",
                            "multi_agent_version": "v2", "node_repl_disabled": False,
                            "apply_patch_tool_type": "freeform",
                            "experimental_supported_tools": ["clock", "send_user_message_async"]}]}
    catalog.write_text(json.dumps(metadata))
    (source / "config.toml").write_text('model="configured-model"\nmodel_catalog_json=' + json.dumps(str(catalog)))
    home = tmp_path / "isolated"
    _isolated_codex_home(home, source)
    config = tomllib.loads((home / "config.toml").read_text())
    copied = Path(config["model_catalog_json"])
    model = json.loads(copied.read_text())["models"][0]
    assert copied.parent == home and copied.stat().st_mode & 0o777 == 0o600
    assert config["model"] == model["slug"] == "configured-model"
    assert model["context_window"] == 272000 and model["input_modalities"] == ["text", "image"]
    assert model["tool_mode"] is None and model["multi_agent_version"] is None
    assert model["apply_patch_tool_type"] is None
    assert model["node_repl_disabled"] is True and model["experimental_supported_tools"] == []
    assert json.loads(catalog.read_text()) == metadata


def test_codex_missing_binary_fails_without_model_or_device(run):
    with pytest.raises(RuntimeError, match="找不到"):
        CodexAgent(executable="nonexistent-test-codex-cli")(build_handoff(run))


def test_codex_adapter_uses_only_summary_and_ephemeral_tools_disabled_config(run, tmp_path, monkeypatch):
    script = tmp_path / "fake-codex"
    decision = json.dumps(choice(), ensure_ascii=False)
    script.write_text(f'''#!{sys.executable}
import json,os,sys,tomllib
from pathlib import Path
args=sys.argv[1:]
assert args[:2]==['--no-daemon','exec'] and '--ephemeral' in args and '--image' not in args
assert '--sandbox' in args and args[args.index('--sandbox')+1]=='read-only'
config=tomllib.loads((Path(os.environ['CODEX_HOME'])/'config.toml').read_text())
assert config['features']['shell_tool'] is False and config['mcp_servers']=={{}}
assert not any(k.startswith('BROWSER_USE_') for k in os.environ)
assert {{k for k in os.environ if k.startswith('CODEX_')}} == {{'CODEX_HOME'}}
assert config['skills']['include_instructions'] is False
prompt=sys.stdin.read()
assert 'private-device' not in prompt and 'private full hierarchy' not in prompt
Path(args[args.index('--output-last-message')+1]).write_text({decision!r})
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':40,'output_tokens':8,'cached_input_tokens':0}}}}))
''')
    script.chmod(0o700)
    monkeypatch.setenv("BROWSER_USE_TEST_SECRET", "not-forwarded")
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/private/desktop-tools")
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "desktop")
    monkeypatch.setenv("CODEX_SESSION_ID", "private-session")
    reply = CodexAgent(executable=str(script), source_home=tmp_path / "empty", timeout=5)(build_handoff(run))
    assert reply.decision == choice() and reply.usage["input_tokens"] == 40


def test_codex_isolation_preserves_explicit_provider_auth_environment(run, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text('model="configured-model"\nmodel_provider="custom"\n'
                                       '[model_providers.custom]\nname="private"\nenv_key="CODEX_OBSERVER_KEY"\n'
                                       'env_http_headers={"X-Auth"="CODEX_OBSERVER_HEADER"}\n')
    script = tmp_path / "fake-codex-auth"
    script.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
assert os.environ['CODEX_OBSERVER_KEY']=='private-key'
assert os.environ['CODEX_OBSERVER_HEADER']=='private-header'
assert 'CODEX_APP_TOOLS_PIPE_PATH' not in os.environ
assert 'private-key' not in sys.stdin.read()
args=sys.argv[1:]
Path(args[args.index('--output-last-message')+1]).write_text({json.dumps(choice())!r})
''')
    script.chmod(0o700)
    monkeypatch.setenv("CODEX_OBSERVER_KEY", "private-key")
    monkeypatch.setenv("CODEX_OBSERVER_HEADER", "private-header")
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "/private/desktop-tools")
    assert CodexAgent(executable=str(script), source_home=source, timeout=5)(build_handoff(run)).decision == choice()


def test_codex_adapter_does_not_accept_tool_execution_events(run, tmp_path):
    script = tmp_path / "fake-codex-tool"
    script.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
sys.stdin.read()
args=sys.argv[1:]
Path(args[args.index('--output-last-message')+1]).write_text({json.dumps(choice())!r})
print(json.dumps({{'type':'item.completed','item':{{'type':'command_execution'}}}}))
''')
    script.chmod(0o700)
    with pytest.raises(RuntimeError, match="使用了工具"):
        CodexAgent(executable=str(script), source_home=tmp_path / "empty", timeout=5)(build_handoff(run))


def test_codex_rejected_product_role_keeps_actual_usage_without_applying(run, tmp_path):
    script = tmp_path / "fake-codex-invalid-role"
    script.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
sys.stdin.read()
args=sys.argv[1:]
Path(args[args.index('--output-last-message')+1]).write_text({json.dumps(choice("perception", candidate="n1", role="products_entry"))!r})
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':40,'output_tokens':8,'cached_input_tokens':0}}}}))
''')
    script.chmod(0o700)
    def apply(*args, **kwargs):
        pytest.fail("an invalid role must not reach the executor")
    result = supervise(run, resume=lambda: pending(run), inspect=lambda: status_for(run), apply=apply,
                       agent=CodexAgent(executable=str(script), source_home=tmp_path / "empty", timeout=5))
    assert result["status"] == "needs_repair" and result["supervisor"]["calls"] == 1
    assert result["supervisor"]["input_tokens"] == 40 and result["supervisor"]["output_tokens"] == 8
    assert "products_entry" in result["stop_reason"]


@pytest.mark.parametrize('second', ['correct', 'repeat', 'budget'])
def test_only_offline_rejection_allows_one_budgeted_correction(run, second):
    from geo_collector.repair_preview import OfflineRejection
    calls, dispatched = [], []
    def agent(context, **kwargs):
        feedback = context['prompt'].get('offline_feedback')
        calls.append(feedback)
        if feedback and second == 'correct':
            assert feedback['dispatched'] is False and feedback['missing_evidence'] == '需要示例区标题'
            return AgentReply(choice('back'))
        return AgentReply(choice())
    def apply(decision, context):
        if decision['decision'] == 'tap':
            raise OfflineRejection('需要示例区标题')
        dispatched.append(decision)
    limits = SupervisorLimits(max_calls_per_task=1 if second == 'budget' else 4)
    result = supervise(run, resume=lambda: {'complete': True} if dispatched else pending(run), inspect=lambda: status_for(run),
                       apply=apply, agent=agent, limits=limits)
    assert len(calls) == result['supervisor']['calls'] == (1 if second == 'budget' else 2)
    assert result['complete'] == bool(dispatched) == (second == 'correct')
