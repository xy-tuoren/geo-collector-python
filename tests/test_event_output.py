import json

from geo_collector.storage import event, event_output


def test_normal_collection_keeps_evidence_without_streaming_to_agent(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    event(path, "frame_captured", frame=1)
    assert json.loads(path.read_text())["frame"] == 1
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_opt_in_events_use_stderr_and_restore_quiet_mode(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    with event_output(True):
        event(path, "frame_captured", frame=1)
        with event_output(False):
            event(path, "frame_captured", frame=2)
        event(path, "frame_captured", frame=3)
    event(path, "frame_captured", frame=4)
    output = capsys.readouterr()
    assert not output.out
    assert [json.loads(line)["frame"] for line in output.err.splitlines()] == [1, 3]
    assert [json.loads(line)["frame"] for line in path.read_text().splitlines()] == [1, 2, 3, 4]
