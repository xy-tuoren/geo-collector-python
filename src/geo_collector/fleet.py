"""Multi-device parallel collection orchestrator with automatic question sharding and tiled mirroring."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from .screen import (
    resolve_scrcpy,
    safe_serial_slug,
    select_adb_devices,
    tile_window_layout,
)
from .storage import read_json, write_json

FLEET_FILE = "fleet.json"


def is_fleet_directory(directory: Path) -> bool:
    return (Path(directory).resolve() / FLEET_FILE).is_file()


def build_shards(serials: list[str], entries: list[str], questions: list[dict]) -> list[dict]:
    """Distribute questions (or entries when single-question) round-robin across ready phones."""
    unique_serials = list(dict.fromkeys(s for s in serials if s))
    unique_entries = list(dict.fromkeys(e for e in entries if e))
    if not unique_serials:
        raise ValueError("多机采集至少需要一台已授权手机")
    if not unique_entries:
        raise ValueError("至少需要选择一个采集入口")
    if not questions:
        raise ValueError("题目列表为空")

    if len(questions) > 1:
        buckets: list[list[dict]] = [[] for _ in unique_serials]
        for idx, item in enumerate(questions):
            buckets[idx % len(unique_serials)].append(item)
        shards = []
        for serial, bucket in zip(unique_serials, buckets):
            if not bucket:
                continue
            shards.append({
                "serial": serial,
                "entries": list(unique_entries),
                "questions": bucket,
                "question_indices": [item["index"] for item in bucket],
            })
        return shards

    if len(unique_entries) > 1 and len(unique_serials) > 1:
        entry_buckets: list[list[str]] = [[] for _ in unique_serials]
        for idx, entry in enumerate(unique_entries):
            entry_buckets[idx % len(unique_serials)].append(entry)
        shards = []
        for serial, e_bucket in zip(unique_serials, entry_buckets):
            if not e_bucket:
                continue
            shards.append({
                "serial": serial,
                "entries": e_bucket,
                "questions": list(questions),
                "question_indices": [questions[0]["index"]],
            })
        return shards

    return [{
        "serial": unique_serials[0],
        "entries": list(unique_entries),
        "questions": list(questions),
        "question_indices": [item["index"] for item in questions],
    }]


def _create_fleet_directory(output_root: Path) -> Path:
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("fleet_%Y%m%d-%H%M%S")
    for idx in range(1, 100):
        name = stamp if idx == 1 else f"{stamp}_{idx}"
        candidate = root / name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"无法在 {root} 创建唯一多机批次目录")


def _parse_last_json(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def _discover_run_directory(device_output: Path | None, parsed: dict | None) -> str | None:
    if isinstance(parsed, dict):
        if isinstance(parsed.get("run_directory"), str) and parsed["run_directory"]:
            return str(Path(parsed["run_directory"]).resolve())
        nxt = parsed.get("next")
        if isinstance(nxt, list) and len(nxt) == 2 and nxt[0] == "handoff" and isinstance(nxt[1], str):
            return str(Path(nxt[1]).resolve())
        req = parsed.get("request")
        if isinstance(req, str) and Path(req).is_file():
            try:
                req_data = read_json(Path(req))
                if isinstance(req_data.get("run_directory"), str):
                    return str(Path(req_data["run_directory"]).resolve())
            except (OSError, ValueError):
                pass
    if device_output and device_output.is_dir():
        batches = sorted(
            (p for p in device_output.iterdir() if p.is_dir() and (p / "调试产物" / "run.json").is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if batches:
            return str(batches[0].resolve())
    return None


def _build_worker_command(
    args,
    *,
    mode: str,
    shard: dict,
    rule_root: Path,
    adb_path: str | None,
    device_output: Path | None = None,
    run_directory: Path | None = None,
    questions_file: Path | None = None,
    scrcpy_path: str | None = None,
    window: dict[str, int] | None = None,
) -> list[str]:
    cmd = [
        sys.executable, "-m", "geo_collector.cli", mode,
        "--serial", shard["serial"],
        "--rule-dir", str(rule_root),
        "--agent", getattr(args, "agent", "none"),
        "--max-agent-calls", str(getattr(args, "max_agent_calls", 12)),
        "--max-agent-calls-per-task", str(getattr(args, "max_agent_calls_per_task", 4)),
        "--agent-timeout", str(getattr(args, "agent_timeout", 120)),
        "--repair", getattr(args, "repair", "pause"),
        "--wait-timeout", str(getattr(args, "wait_timeout", 180)),
    ]
    if adb_path:
        cmd.extend(["--adb", adb_path])
    if getattr(args, "events", False):
        cmd.append("--events")
    if getattr(args, "retry_agent_error", False):
        cmd.append("--retry-agent-error")
    for step in getattr(args, "teach", []) or []:
        cmd.extend(["--teach", step])

    if getattr(args, "screen_control", False):
        cmd.append("--screen-control")
    elif getattr(args, "screen", False):
        cmd.append("--screen")
    if scrcpy_path:
        cmd.extend(["--scrcpy", scrcpy_path])
    if window is not None and (getattr(args, "screen", False) or getattr(args, "screen_control", False)):
        cmd.extend([
            "--screen-window",
            str(window["window_x"]),
            str(window["window_y"]),
            str(window["window_width"]),
        ])

    if mode == "run":
        if questions_file is not None:
            cmd.extend(["--questions-file", str(questions_file)])
            cmd.extend(["--question-index", ",".join(str(i) for i in shard["question_indices"])])
        else:
            cmd.extend(["--question", shard["questions"][0]["text"]])
        cmd.append("--entry")
        cmd.extend(shard["entries"])
        if device_output is None:
            raise ValueError("worker run 缺少输出目录")
        cmd.extend(["--output", str(device_output)])
        if getattr(args, "learn", False):
            cmd.append("--learn")
    elif mode == "resume":
        if run_directory is None:
            raise ValueError("worker resume 缺少批次目录")
        cmd.append(str(run_directory))
    else:
        raise ValueError(f"不支持的 worker 模式：{mode}")
    return cmd


def _terminate_workers(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 3.0
    for proc in procs:
        if proc.poll() is None:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass


def _execute_workers(fleet_dir: Path, worker_specs: list[dict], *, spawn=None) -> list[dict]:
    logs_dir = fleet_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "event": "fleet_started",
                "fleet_directory": str(fleet_dir),
                "workers": [
                    {
                        "serial": spec["shard"]["serial"],
                        "mode": spec["mode"],
                        "entries": spec["shard"]["entries"],
                        "question_indices": spec["shard"]["question_indices"],
                    }
                    for spec in worker_specs
                ],
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )

    if spawn is not None:
        results = []
        for spec in worker_specs:
            slug = safe_serial_slug(spec["shard"]["serial"])
            out_log = logs_dir / f"{slug}.stdout.log"
            err_log = logs_dir / f"{slug}.stderr.log"
            rc, stdout_text, stderr_text = spawn(spec["command"], spec)
            out_log.write_text(stdout_text or "", encoding="utf-8")
            err_log.write_text(stderr_text or "", encoding="utf-8")
            results.append(_summarize_worker(spec, rc, stdout_text or "", stderr_text or "", out_log, err_log))
        return results

    launched = []
    streams = []
    try:
        for spec in worker_specs:
            slug = safe_serial_slug(spec["shard"]["serial"])
            out_log = logs_dir / f"{slug}.stdout.log"
            err_log = logs_dir / f"{slug}.stderr.log"
            out_fp = out_log.open("wb")
            err_fp = err_log.open("wb")
            streams.extend([out_fp, err_fp])
            proc = subprocess.Popen(spec["command"], stdin=subprocess.DEVNULL, stdout=out_fp, stderr=err_fp)
            launched.append((spec, proc, out_log, err_log))
        try:
            for _, proc, _, _ in launched:
                proc.wait()
        except KeyboardInterrupt:
            _terminate_workers([proc for _, proc, _, _ in launched])
            raise
    finally:
        for fp in streams:
            try:
                fp.close()
            except OSError:
                pass

    results = []
    for spec, proc, out_log, err_log in launched:
        stdout_text = out_log.read_text(encoding="utf-8", errors="replace") if out_log.is_file() else ""
        stderr_text = err_log.read_text(encoding="utf-8", errors="replace") if err_log.is_file() else ""
        results.append(_summarize_worker(spec, proc.returncode, stdout_text, stderr_text, out_log, err_log))
    return results


def _summarize_worker(
    spec: dict,
    returncode: int,
    stdout_text: str,
    stderr_text: str,
    out_log: Path,
    err_log: Path,
) -> dict:
    shard = spec["shard"]
    parsed = _parse_last_json(stdout_text)
    run_dir = _discover_run_directory(spec.get("device_output"), parsed) or (
        str(spec["run_directory"]) if spec.get("run_directory") else None
    )
    if returncode == 0 and isinstance(parsed, dict) and parsed.get("complete") is True:
        status = "complete"
    elif returncode == 20 or (isinstance(parsed, dict) and parsed.get("status") == "needs_repair"):
        status = "needs_repair"
    else:
        status = "failed"
    row = {
        "serial": shard["serial"],
        "mode": spec["mode"],
        "status": status,
        "exit_code": returncode,
        "entries": shard["entries"],
        "question_indices": shard["question_indices"],
        "run_directory": run_dir,
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }
    if isinstance(parsed, dict):
        if "captured" in parsed:
            row["captured"] = parsed["captured"]
        if "entry_not_found" in parsed:
            row["entry_not_found"] = parsed["entry_not_found"]
        if "tasks" in parsed:
            row["tasks"] = parsed["tasks"]
        if status == "needs_repair":
            row["handoff"] = parsed
        elif status == "failed":
            row["error"] = parsed.get("error") or parsed.get("problems") or stderr_text.strip()[-400:]
    elif status == "failed":
        row["error"] = stderr_text.strip()[-400:] or f"worker 退出码 {returncode}"
    return row


def _aggregate_status(device_rows: list[dict]) -> tuple[str, bool]:
    if device_rows and all(row["status"] in {"complete", "skipped_complete"} for row in device_rows):
        return "complete", True
    if any(row["status"] == "needs_repair" for row in device_rows) and not any(
        row["status"] == "failed" for row in device_rows
    ):
        return "needs_repair", False
    return "incomplete", False


def run_fleet(args, *, spawn=None) -> dict:
    """Shard questions across multiple ADB devices and execute parallel `run` workers."""
    from .cli import questions as parse_questions

    if getattr(args, "capture_current_answer", False):
        raise ValueError("--capture-current-answer 仅支持单台手机，请使用 --serial")
    if getattr(args, "serial", None) and (getattr(args, "all_devices", False) or getattr(args, "serials", None)):
        raise ValueError("--serial 与 --serials / --all-devices 不能同时使用")

    items = parse_questions(args)
    entries = list(dict.fromkeys(args.entry))
    adb_path, selected_serials = select_adb_devices(
        serials=getattr(args, "serials", None),
        all_devices=bool(getattr(args, "all_devices", False)),
        adb=args.adb,
    )
    shards = build_shards(selected_serials, entries, items)
    unused_serials = [s for s in selected_serials if s not in {shard["serial"] for shard in shards}]

    rule_root = (args.rule_dir or Path(".geo-collector")).resolve()
    questions_file = args.questions_file.resolve() if args.questions_file else None
    mirror_enabled = bool(getattr(args, "screen", False) or getattr(args, "screen_control", False))
    scrcpy_bin = None
    windows: list[dict[str, int] | None] = [None] * len(shards)
    if mirror_enabled:
        # Resolve/install scrcpy once before spawning workers so they never race on downloads.
        inst = resolve_scrcpy(scrcpy_path=getattr(args, "scrcpy", None), auto_install=True)
        scrcpy_bin = inst.executable
        windows = tile_window_layout(
            len(shards),
            window_width=getattr(args, "window_width", 360),
            columns=getattr(args, "columns", None),
        )

    fleet_dir = _create_fleet_directory(args.output)
    state = {
        "fleet_version": 1,
        "status": "running",
        "created_at": time.time(),
        "adb": adb_path,
        "rule_root": str(rule_root),
        "entries": entries,
        "questions": items,
        "questions_file": str(questions_file) if questions_file else None,
        "selected_serials": selected_serials,
        "unused_serials": unused_serials,
        "screen": mirror_enabled,
        "screen_control": bool(getattr(args, "screen_control", False)),
        "shards": shards,
        "devices": [],
    }
    write_json(fleet_dir / FLEET_FILE, state)

    worker_specs = []
    for shard, window in zip(shards, windows):
        slug = safe_serial_slug(shard["serial"])
        device_output = fleet_dir / "devices" / slug
        device_output.mkdir(parents=True, exist_ok=True)
        cmd = _build_worker_command(
            args,
            mode="run",
            shard=shard,
            rule_root=rule_root,
            adb_path=adb_path,
            device_output=device_output,
            questions_file=questions_file,
            scrcpy_path=scrcpy_bin,
            window=window,
        )
        worker_specs.append({
            "mode": "run",
            "shard": shard,
            "device_output": device_output,
            "run_directory": None,
            "window": window,
            "command": cmd,
        })

    device_rows = _execute_workers(fleet_dir, worker_specs, spawn=spawn)
    status, complete = _aggregate_status(device_rows)
    state.update(status=status, complete=complete, updated_at=time.time(), devices=device_rows)
    write_json(fleet_dir / FLEET_FILE, state)
    return {
        "status": status,
        "complete": complete,
        "fleet_directory": str(fleet_dir),
        "device_count": len(device_rows),
        "unused_serials": unused_serials,
        "captured": sum(int(r.get("captured", 0)) for r in device_rows),
        "entry_not_found": sum(int(r.get("entry_not_found", 0)) for r in device_rows),
        "devices": device_rows,
    }


def resume_fleet(args, fleet_directory: Path, *, spawn=None) -> dict:
    """Resume only unfinished device batches inside an existing fleet directory."""
    from .cli import verify_run

    fleet_dir = Path(fleet_directory).resolve()
    state = read_json(fleet_dir / FLEET_FILE)
    rule_root = Path(state["rule_root"]).resolve()
    if getattr(args, "rule_dir", None) and args.rule_dir.resolve() != rule_root:
        raise ValueError("恢复多机批次必须使用原规则库")

    existing_by_serial = {row["serial"]: row for row in state.get("devices", []) if isinstance(row, dict)}
    shards = state.get("shards", [])
    if not shards:
        raise ValueError("fleet.json 中没有可恢复的设备分片")

    adb_path = args.adb or state.get("adb")
    questions_file = Path(state["questions_file"]) if state.get("questions_file") else None
    completed_rows: dict[str, dict] = {}
    pending_shards: list[tuple[dict, Path | None, Path]] = []

    for shard in shards:
        serial = shard["serial"]
        prev = existing_by_serial.get(serial, {})
        run_dir_str = prev.get("run_directory")
        run_dir = Path(run_dir_str).resolve() if isinstance(run_dir_str, str) and run_dir_str else None
        if run_dir and (run_dir / "调试产物" / "run.json").is_file():
            check = verify_run(run_dir)
            if check.get("complete") is True:
                completed_rows[serial] = {
                    **prev,
                    "serial": serial,
                    "status": "skipped_complete",
                    "exit_code": 0,
                    "run_directory": str(run_dir),
                    "captured": check.get("captured", prev.get("captured", 0)),
                    "entry_not_found": check.get("entry_not_found", prev.get("entry_not_found", 0)),
                    "tasks": check.get("tasks", prev.get("tasks", 0)),
                }
                continue
        slug = safe_serial_slug(serial)
        device_output = fleet_dir / "devices" / slug
        device_output.mkdir(parents=True, exist_ok=True)
        pending_shards.append((shard, run_dir if (run_dir and (run_dir / "调试产物" / "run.json").is_file()) else None, device_output))

    if not pending_shards:
        rows = [completed_rows[shard["serial"]] for shard in shards]
        state.update(status="complete", complete=True, updated_at=time.time(), devices=rows)
        write_json(fleet_dir / FLEET_FILE, state)
        return {
            "status": "complete",
            "complete": True,
            "fleet_directory": str(fleet_dir),
            "device_count": len(rows),
            "resumed_devices": 0,
            "captured": sum(int(r.get("captured", 0)) for r in rows),
            "entry_not_found": sum(int(r.get("entry_not_found", 0)) for r in rows),
            "devices": rows,
        }

    mirror_enabled = bool(getattr(args, "screen", False) or getattr(args, "screen_control", False))
    scrcpy_bin = None
    windows: list[dict[str, int] | None] = [None] * len(pending_shards)
    if mirror_enabled:
        inst = resolve_scrcpy(scrcpy_path=getattr(args, "scrcpy", None), auto_install=True)
        scrcpy_bin = inst.executable
        windows = tile_window_layout(
            len(pending_shards),
            window_width=getattr(args, "window_width", 360),
            columns=getattr(args, "columns", None),
        )

    worker_specs = []
    for (shard, run_dir, device_output), window in zip(pending_shards, windows):
        mode = "resume" if run_dir is not None else "run"
        cmd = _build_worker_command(
            args,
            mode=mode,
            shard=shard,
            rule_root=rule_root,
            adb_path=adb_path,
            device_output=device_output if mode == "run" else None,
            run_directory=run_dir,
            questions_file=questions_file if mode == "run" else None,
            scrcpy_path=scrcpy_bin,
            window=window,
        )
        worker_specs.append({
            "mode": mode,
            "shard": shard,
            "device_output": device_output,
            "run_directory": run_dir,
            "window": window,
            "command": cmd,
        })

    ran_rows = {row["serial"]: row for row in _execute_workers(fleet_dir, worker_specs, spawn=spawn)}
    merged_rows = [
        ran_rows[shard["serial"]] if shard["serial"] in ran_rows else completed_rows[shard["serial"]]
        for shard in shards
    ]
    status, complete = _aggregate_status(merged_rows)
    state.update(status=status, complete=complete, updated_at=time.time(), devices=merged_rows)
    write_json(fleet_dir / FLEET_FILE, state)
    return {
        "status": status,
        "complete": complete,
        "fleet_directory": str(fleet_dir),
        "device_count": len(merged_rows),
        "resumed_devices": len(pending_shards),
        "captured": sum(int(r.get("captured", 0)) for r in merged_rows),
        "entry_not_found": sum(int(r.get("entry_not_found", 0)) for r in merged_rows),
        "devices": merged_rows,
    }


def verify_fleet(fleet_directory: Path) -> dict:
    """Verify every device batch in a fleet and confirm full non-overlapping task coverage."""
    from .cli import verify_run

    fleet_dir = Path(fleet_directory).resolve()
    state = read_json(fleet_dir / FLEET_FILE)
    expected = {
        (entry, item["index"], item["text"])
        for entry in state.get("entries", [])
        for item in state.get("questions", [])
    }
    problems: list[str] = []
    if not expected:
        problems.append("多机批次预期任务列表为空")

    by_serial = {row["serial"]: row for row in state.get("devices", []) if isinstance(row, dict)}
    seen_tasks: list[tuple[str, int, str]] = []
    device_reports: list[dict] = []
    total_captured = total_not_found = total_tasks = 0

    for shard in state.get("shards", []):
        serial = shard["serial"]
        row = by_serial.get(serial, {})
        run_dir_str = row.get("run_directory")
        if not isinstance(run_dir_str, str) or not run_dir_str:
            problems.append(f"{serial} 缺少子批次目录")
            device_reports.append({"serial": serial, "complete": False, "problems": ["缺少子批次目录"]})
            continue
        run_dir = Path(run_dir_str).resolve()
        run_json = run_dir / "调试产物" / "run.json"
        if not run_json.is_file():
            problems.append(f"{serial} 缺少 run.json：{run_dir}")
            device_reports.append({"serial": serial, "complete": False, "run_directory": str(run_dir), "problems": ["缺少 run.json"]})
            continue
        run_state = read_json(run_json)
        for task in run_state.get("tasks", []):
            seen_tasks.append((task["entry"], task["index"], task["text"]))
        report = verify_run(run_dir)
        total_tasks += report.get("tasks", 0)
        total_captured += report.get("captured", 0)
        total_not_found += report.get("entry_not_found", 0)
        if not report.get("complete"):
            for item in report.get("problems", []):
                problems.append(f"{serial}: {item}")
        device_reports.append({"serial": serial, **report})

    if expected and (set(seen_tasks) != expected or len(seen_tasks) != len(expected)):
        missing = expected - set(seen_tasks)
        extra = set(seen_tasks) - expected
        if missing:
            problems.append(f"多机汇总缺少 {len(missing)} 个任务")
        if extra:
            problems.append(f"多机汇总出现 {len(extra)} 个预期外任务")
        if len(seen_tasks) != len(set(seen_tasks)):
            problems.append("多机汇总出现重复任务")

    return {
        "complete": not problems,
        "fleet_directory": str(fleet_dir),
        "device_count": len(device_reports),
        "tasks": total_tasks,
        "captured": total_captured,
        "entry_not_found": total_not_found,
        "devices": device_reports,
        "problems": problems,
    }
