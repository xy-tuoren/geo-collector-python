"""Tests for multi-device discovery, tiled multi-screen mirroring, fleet sharding, and concurrency safety."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from geo_collector import cli, device, fleet, screen
from geo_collector.lessons import LessonStore
from geo_collector.rules import RuleStore
from geo_collector.storage import read_json, write_json


def _make_complete_device_batch(directory: Path, serial: str, entries: list[str], questions: list[dict]) -> Path:
    batch = directory / f"batch_{serial}"
    tasks = []
    for entry in entries:
        for item in questions:
            tasks.append({"entry": entry, "index": item["index"], "text": item["text"], "status": "complete"})
            img = batch / "交付图片" / entry / f"{item['index']:03d}" / "answer.png"
            img.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (120, 260), "white").save(img, format="PNG")
            ans_file = batch / "调试产物" / entry / f"{item['index']:03d}" / "answer.json"
            frame = ans_file.parent / "capture-test" / "frame-001.png"
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(img.read_bytes())
            write_json(ans_file, {
                "entry": entry,
                "index": item["index"],
                "question": item["text"],
                "question_confirmed": True,
                "citations_checked": True,
                "citations_found": False,
                "citations_expanded": False,
                "completion_confirmed": True,
                "end_confirmed": True,
                "occlusion_checked": True,
                "frame_count": 1,
                "seams": [],
                "products": {"present": False, "complete": True},
                "image_paths": [str(img)],
                "frame_files": [str(frame)],
            })
    write_json(batch / "调试产物" / "run.json", {
        "artifact_layout_version": 1,
        "status": "complete",
        "serial": serial,
        "entries": entries,
        "questions": questions,
        "tasks": tasks,
        "agent_requests": 0,
    })
    return batch


def test_list_adb_devices_and_cli_devices_command(monkeypatch):
    raw = (
        b"List of devices attached\n"
        b"DEV-A device usb:0-1 product:p1 model:Redmi_A device:a transport_id:1\n"
        b"DEV-B unauthorized usb:0-2 transport_id:2\n"
        b"DEV-C device usb:0-3 model:Pixel_C\n"
    )
    monkeypatch.setattr(screen, "_resolve_adb", lambda adb=None: "/opt/adb")
    monkeypatch.setattr(screen.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=raw))

    out = cli.execute(cli.parser().parse_args(["devices"]))
    assert out["adb"] == "/opt/adb"
    assert out["ready_count"] == 2
    assert [d["serial"] for d in out["devices"]] == ["DEV-A", "DEV-B", "DEV-C"]
    assert out["devices"][0]["model"] == "Redmi_A" and out["devices"][0]["ready"] is True
    assert out["devices"][1]["ready"] is False

    adb_path, all_ready = screen.select_adb_devices(all_devices=True)
    assert adb_path == "/opt/adb" and all_ready == ["DEV-A", "DEV-C"]

    _, chosen = screen.select_adb_devices(serials="DEV-C,DEV-A,DEV-C")
    assert chosen == ["DEV-C", "DEV-A"]

    with pytest.raises(device.DeviceError, match="DEV-B"):
        screen.select_adb_devices(serials="DEV-A,DEV-B")

    with pytest.raises(ValueError, match="不能同时使用"):
        screen.select_adb_devices(serial="DEV-A", all_devices=True)


def test_tile_window_layout_and_multi_screen_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    layout = screen.tile_window_layout(5, window_width=320, columns=3)
    assert len(layout) == 5
    # Row 0: cols 0, 1, 2
    assert layout[0]["window_x"] < layout[1]["window_x"] < layout[2]["window_x"]
    assert layout[0]["window_y"] == layout[1]["window_y"] == layout[2]["window_y"]
    # Row 1 wraps back to col 0 with larger Y
    assert layout[3]["window_x"] == layout[0]["window_x"]
    assert layout[3]["window_y"] > layout[0]["window_y"]

    monkeypatch.setattr(screen.shutil, "which", lambda name: "/opt/bin/scrcpy" if name == "scrcpy" else None)
    monkeypatch.setattr(
        screen,
        "select_adb_devices",
        lambda **kw: ("/opt/bin/adb", ["DEV-1", "DEV-2"]),
    )

    # Preview mode
    dry = cli.execute(cli.parser().parse_args(["screen", "--all", "--window-width", "340", "--dry-run"]))
    assert dry["status"] == "preview" and dry["count"] == 2 and dry["readonly"] is True
    assert any(arg == "--window-width=340" for arg in dry["mirrors"][0]["command"])
    assert any(arg.startswith("--window-x=") for arg in dry["mirrors"][1]["command"])
    assert dry["mirrors"][0]["window"]["window_x"] < dry["mirrors"][1]["window"]["window_x"]

    # Background launch spawns 2 tiled processes with separate per-serial logs
    spawned = []
    terminated = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            self.pid = 5000 + len(spawned)
            self.returncode = None
            spawned.append((cmd, kwargs))

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            terminated.append(self.pid)
            self.returncode = 0

    monkeypatch.setattr(screen.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(screen.time, "sleep", lambda _: None)

    started = cli.execute(cli.parser().parse_args(["screen", "--serials", "DEV-1,DEV-2", "--background", "--control"]))
    assert started["status"] == "started" and started["count"] == 2 and started["readonly"] is False
    assert started["mirrors"][0]["log_file"].endswith("screen-DEV-1.log")
    assert started["mirrors"][1]["log_file"].endswith("screen-DEV-2.log")
    assert len(spawned) == 2

    # Rollback: if the second scrcpy fails on startup, the first one is cleanly terminated.
    spawned.clear()
    terminated.clear()
    attempt = {"n": 0}

    class SecondFailsProc(FakeProc):
        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            attempt["n"] += 1
            if attempt["n"] == 2:
                self.returncode = 1

    monkeypatch.setattr(screen.subprocess, "Popen", SecondFailsProc)
    with pytest.raises(screen.ScreenError, match="启动失败"):
        cli.execute(cli.parser().parse_args(["screen", "--all", "--background"]))
    assert terminated == [5000]


def test_build_shards_round_robin_questions_and_entries():
    qs = [{"index": i, "text": f"Q{i}"} for i in (1, 3, 5, 7, 9)]
    shards = fleet.build_shards(["A", "B"], ["xiaohe"], qs)
    assert [(s["serial"], s["question_indices"]) for s in shards] == [("A", [1, 5, 9]), ("B", [3, 7])]

    # More devices than questions -> unused devices get no empty shard
    shards_fewer = fleet.build_shards(["A", "B", "C"], ["xiaohe"], qs[:2])
    assert [(s["serial"], s["question_indices"]) for s in shards_fewer] == [("A", [1]), ("B", [3])]

    # Single question across multiple entries & devices -> shards by entry
    shards_entries = fleet.build_shards(["A", "B"], ["xiaohe", "douyin"], [qs[0]])
    assert [(s["serial"], s["entries"], s["question_indices"]) for s in shards_entries] == [
        ("A", ["xiaohe"], [1]),
        ("B", ["douyin"], [1]),
    ]


def test_same_second_new_run_creates_distinct_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "strftime", lambda fmt: "batch_20261124-223000")
    out = tmp_path / "captures"
    d1, _ = cli.new_run(cli.parser().parse_args(["run", "--question", "甲", "--serial", "DEV-A", "--output", str(out)]))
    d2, _ = cli.new_run(cli.parser().parse_args(["run", "--question", "乙", "--serial", "DEV-B", "--output", str(out)]))
    d3, _ = cli.new_run(cli.parser().parse_args(["run", "--question", "丙", "--serial", "DEV-B", "--output", str(out)]))
    assert len({d1, d2, d3}) == 3
    assert d1.name == "batch_20261124-223000"
    assert d2.name == "batch_20261124-223000_DEV-B"
    assert d3.name == "batch_20261124-223000_2"


def test_fleet_run_verify_and_resume_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    qfile = tmp_path / "questions.txt"
    qfile.write_text("问题一\n问题二\n问题三\n", encoding="utf-8")

    monkeypatch.setattr(fleet, "select_adb_devices", lambda **kw: ("/opt/adb", ["DEV-A", "DEV-B"]))
    monkeypatch.setattr(
        fleet,
        "resolve_scrcpy",
        lambda **kw: screen.ScrcpyInstallation(executable="/opt/scrcpy", source="system"),
    )

    commands_seen = []

    def fake_spawn_initial(cmd, spec):
        commands_seen.append((spec["shard"]["serial"], cmd))
        serial = spec["shard"]["serial"]
        if serial == "DEV-A":
            batch = _make_complete_device_batch(
                spec["device_output"], serial, spec["shard"]["entries"], spec["shard"]["questions"]
            )
            payload = cli.verify_run(batch)
            return 0, json.dumps(payload, ensure_ascii=False), ""
        # DEV-B pauses with exit 20 for repair
        batch = spec["device_output"] / f"batch_{serial}"
        write_json(batch / "调试产物" / "run.json", {
            "artifact_layout_version": 1,
            "status": "paused",
            "serial": serial,
            "entries": spec["shard"]["entries"],
            "questions": spec["shard"]["questions"],
            "tasks": [{"entry": "xiaohe", "index": 2, "text": "问题二", "status": "pending"}],
        })
        handoff = {"status": "needs_repair", "exit_code": 20, "next": ["handoff", str(batch)]}
        return 20, json.dumps(handoff, ensure_ascii=False), ""

    monkeypatch.setattr(fleet, "_execute_workers", lambda fd, specs, spawn=None: _orig_exec(fd, specs, spawn=fake_spawn_initial))
    _orig_exec = fleet._execute_workers
    # Restore and call directly via run_fleet(spawn=...)
    monkeypatch.undo()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(fleet, "select_adb_devices", lambda **kw: ("/opt/adb", ["DEV-A", "DEV-B"]))
    monkeypatch.setattr(
        fleet,
        "resolve_scrcpy",
        lambda **kw: screen.ScrcpyInstallation(executable="/opt/scrcpy", source="system"),
    )

    run_args = cli.parser().parse_args([
        "run", "--all-devices", "--questions-file", str(qfile),
        "--entry", "xiaohe", "--output", str(tmp_path / "out"),
        "--rule-dir", str(tmp_path / "rules"), "--screen",
    ])
    res = fleet.run_fleet(run_args, spawn=fake_spawn_initial)
    assert res["status"] == "needs_repair" and res["complete"] is False
    assert res["device_count"] == 2
    assert len(commands_seen) == 2
    # Both workers received tiled --screen-window and pre-resolved --scrcpy
    for _, cmd in commands_seen:
        assert "--screen" in cmd and "--scrcpy" in cmd and "/opt/scrcpy" in cmd and "--screen-window" in cmd
    idx_a = commands_seen[0][1].index("--screen-window")
    idx_b = commands_seen[1][1].index("--screen-window")
    assert int(commands_seen[0][1][idx_a + 1]) < int(commands_seen[1][1][idx_b + 1])

    fleet_dir = Path(res["fleet_directory"])
    assert fleet.is_fleet_directory(fleet_dir)
    # verify via CLI detects fleet.json and reports incomplete until DEV-B finishes
    assert cli.main(["verify", str(fleet_dir)]) == 1
    verify_before = json.loads(capsys.readouterr().out)
    assert verify_before["complete"] is False

    # Now resume fleet: DEV-A should be skipped (already complete), only DEV-B resumes!
    resumed_cmds = []

    def fake_spawn_resume(cmd, spec):
        resumed_cmds.append((spec["shard"]["serial"], spec["mode"], cmd))
        batch = _make_complete_device_batch(
            spec["device_output"], spec["shard"]["serial"], spec["shard"]["entries"], spec["shard"]["questions"]
        )
        # Overwrite the paused run directory in place
        target = spec["run_directory"]
        for src in batch.rglob("*"):
            rel = src.relative_to(batch)
            dst = target / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        # Rewrite answer.json image_paths/frame_files to point inside target
        ans_file = target / "调试产物" / "xiaohe" / "002" / "answer.json"
        ans = read_json(ans_file)
        ans["image_paths"] = [str(target / "交付图片" / "xiaohe" / "002" / "answer.png")]
        ans["frame_files"] = [str(target / "调试产物" / "xiaohe" / "002" / "capture-test" / "frame-001.png")]
        write_json(ans_file, ans)
        payload = cli.verify_run(target)
        return 0, json.dumps(payload, ensure_ascii=False), ""

    resume_args = cli.parser().parse_args(["resume", str(fleet_dir), "--screen"])
    resumed = fleet.resume_fleet(resume_args, fleet_dir, spawn=fake_spawn_resume)
    assert resumed["status"] == "complete" and resumed["complete"] is True
    assert resumed["resumed_devices"] == 1
    assert [row[0] for row in resumed_cmds] == ["DEV-B"]
    assert resumed_cmds[0][1] == "resume"

    assert cli.main(["verify", str(fleet_dir)]) == 0
    verify_after = json.loads(capsys.readouterr().out)
    assert verify_after["complete"] is True and verify_after["tasks"] == 3 and verify_after["captured"] == 3


def test_rule_and_lesson_stores_use_cross_process_lock_files(tmp_path):
    from geo_collector.device import Observation

    rules = RuleStore(tmp_path / "rules")
    obs = Observation(b"", "<hierarchy/>", "com.demo", "Main", (720, 1600), (720, 1600), "1.0")
    rules.promote("com.demo", "demo.step", {"actions": [{"type": "tap", "target": {"kind": "node", "attr": "text", "value": "OK"}}]}, obs, "test")
    assert (tmp_path / "rules" / ".rules.lock").is_file()

    lessons = LessonStore(tmp_path / "rules")
    saved = lessons.add({"note": "多机并发测试经验", "strategy": "note"})
    assert saved["id"]
    assert (tmp_path / "rules" / ".lessons.lock").is_file()
