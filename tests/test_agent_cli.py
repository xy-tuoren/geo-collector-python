import json

import pytest

from geo_collector import cli
from geo_collector.storage import event


def test_ai_supervision_is_explicit_for_normal_runs():
    parse = cli.parser().parse_args
    assert parse(["run", "--question", "示例问题"]).agent == "none"
    assert parse(["resume", "example"]).agent == "none"
    assert parse(["supervise", "example"]).agent == "codex"
    options = parse(["run", "--question", "示例问题", "--agent", "codex", "--max-agent-calls", "3"])
    assert options.max_agent_calls == 3 and not options.events


def test_invalid_agent_budget_stops_before_creating_run_or_device(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid budget must fail before creating a run")
    monkeypatch.setattr(cli, "new_run", forbidden)
    options = cli.parser().parse_args(["run", "--question", "示例问题", "--agent", "codex", "--max-agent-calls", "0"])
    with pytest.raises(ValueError, match="正整数"):
        cli.execute(options)


def test_agent_loop_does_not_allow_a_second_waiting_controller():
    options = cli.parser().parse_args(["resume", "example", "--agent", "codex", "--repair", "wait"])
    with pytest.raises(ValueError, match="默认 --repair pause"):
        cli.execute(options)


def test_default_stdout_is_one_final_json_even_when_collector_records_many_events(tmp_path, monkeypatch, capsys):
    def collect(args):
        for index in range(20):
            event(tmp_path / "events.jsonl", "frame_captured", frame=index)
        return {"complete": True, "captured": 1}
    monkeypatch.setattr(cli, "execute", collect)
    assert cli.main(["run", "--question", "示例问题"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"complete": True, "captured": 1}
    assert not output.err
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 20


def test_supervisor_pause_returns_twenty_and_one_parseable_summary(monkeypatch, capsys):
    monkeypatch.setattr(cli, "execute", lambda args: {"status": "needs_repair", "complete": False, "stop_reason": "预算已用完"})
    assert cli.main(["supervise", "example"]) == 20
    assert json.loads(capsys.readouterr().out)["stop_reason"] == "预算已用完"


def test_runtime_failure_preserves_the_single_stdout_result_contract(monkeypatch, capsys):
    def fail(args):
        raise RuntimeError("手机连接已断开")
    monkeypatch.setattr(cli, "execute", fail)
    assert cli.main(["resume", "example"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "failed", "error": "手机连接已断开"}
    assert not output.err


def test_entries_query_never_initializes_a_device(monkeypatch):
    from geo_collector import device
    monkeypatch.setattr(device, "Device", lambda **kwargs: pytest.fail("entries is offline"))
    result = cli.execute(cli.parser().parse_args(["entries"]))
    assert {item["id"] for item in result["entries"]} == {"xiaohe", "douyin", "toutiao", "douyin-direct"}
    assert sum(item["default_selected"] for item in result["entries"]) == 2
