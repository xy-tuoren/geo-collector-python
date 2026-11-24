from __future__ import annotations

import argparse
import hashlib
import json
import sys
import statistics
import time
from pathlib import Path

from .recovery import NeedsRepair, save_observation, submit_repair
from .platforms import PLATFORMS
from .rules import RuleStore
from .storage import DeviceLease, event_output, read_json, write_json


def parser():
    from .setup import MIRRORS, package_version

    root = argparse.ArgumentParser(description="Python 手机采集：规则自动运行，当前 Agent 按需修复，无额外模型配置。")
    root.add_argument("--version", action="version", version=f"geo-collect {package_version()}")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("doctor", "inspect", "run", "resume", "recover", "supervise"):
        command = commands.add_parser(name)
        command.add_argument("--serial")
        command.add_argument("--adb")
        command.add_argument("--rule-dir", type=Path)
        if name in {"doctor", "inspect"}:
            command.add_argument("--output", type=Path, default=Path("captures/inspect"))
        else:
            command.add_argument("--events", action="store_true", help="将逐步事件输出到stderr；默认仅保存本地日志")
            command.add_argument("--agent", choices=["none", "codex"], default="codex" if name == "supervise" else "none",
                                 help="仅未知页面调用已有Codex CLI；正常采集不调用模型")
            command.add_argument("--max-agent-calls", type=int, default=12, help="同一批次累计AI调用上限，恢复时不清零")
            command.add_argument("--max-agent-calls-per-task", type=int, default=4, help="同一道题累计AI调用上限")
            command.add_argument("--agent-timeout", type=int, default=120, help="单次AI调用超时秒数")
            command.add_argument("--retry-agent-error", action="store_true", help="显式重试此前失败且未派发建议的AI调用，保留累计预算")
            command.add_argument("--repair", choices=["pause", "wait"], default="pause", help="未知页面默认退出并保存断点；wait供正在运行的Agent接管")
            command.add_argument("--wait-timeout", type=int, default=180)
            command.add_argument("--teach", action="append", default=[], help="强制由Agent示教指定步骤，如citation.expand")
            if name == "run":
                command.add_argument("--questions-file", type=Path)
                command.add_argument("--question", help="单题，与questions-file二选一")
                command.add_argument("--question-index", default="", help="从1开始，逗号分隔，如1,5,12")
                command.add_argument("--entry", nargs="+", choices=list(PLATFORMS), default=["xiaohe", "douyin"])
                command.add_argument("--capture-current-answer", action="store_true", help="只采当前回答，不输入/发送/新会话；需指定一题和一个入口")
                command.add_argument("--learn", action="store_true", help="由当前Agent首次探索入口并识别页面角色；整题验收后保存复用")
                command.add_argument("--output", type=Path, default=Path("captures"))
            else:
                command.add_argument("run_directory", type=Path)
                if name == "recover":
                    command.add_argument("--action", choices=["retry-current", "reenter-answer", "restart-app", "recapture-body"], required=True)
                    command.add_argument("--reason", required=True)
    repair = commands.add_parser("repair", help="当前Agent查看request截图后提交定位；验证成功后保存为规则")
    repair.add_argument("request", type=Path)
    repair.add_argument("--sha256", required=True)
    repair.add_argument("--preview", action="store_true", help="离线复验原始现场，可反复试选，不写入响应或操作手机")
    repair.add_argument("--role", help="将候选绑定为页面角色，省略则预备一次探索点击")
    target = repair.add_mutually_exclusive_group()
    target.add_argument("--candidate", help="handoff 中与原始截图绑定的候选 ID")
    target.add_argument("--point", type=float, nargs=2, metavar=("X", "Y"), help="原始PNG物理坐标，自动提取锚点或模板")
    target.add_argument("--plan", type=Path, help="tap/back/swipe动作，或有节点锚点的layout修复方案")
    rules = commands.add_parser("rules", help="列出规则及健康度：成功/失败次数、已验证版本、失活原因")
    rules.add_argument("--rule-dir", type=Path, default=Path(".geo-collector"))
    lessons = commands.add_parser("lessons", help="管理本地排障经验库 lessons.json（离线，不连接手机）")
    lessons.add_argument("--rule-dir", type=Path, default=None)
    lessons.add_argument("--all", dest="include_inactive", action="store_true", help="包含已停用的历史经验")
    lesson_actions = lessons.add_subparsers(dest="lesson_action")
    lesson_list = lesson_actions.add_parser("list", help="列出经验库条目")
    lesson_list.add_argument("--rule-dir", dest="sub_rule_dir", type=Path, default=None)
    lesson_list.add_argument("--all", dest="sub_include_inactive", action="store_true")
    lesson_add = lesson_actions.add_parser("add", help="新增或更新一条排障经验")
    lesson_add.add_argument("--rule-dir", dest="sub_rule_dir", type=Path, default=None)
    lesson_add.add_argument("--note", required=True, help="简短排障经验说明（2～240字）")
    lesson_add.add_argument("--strategy", default="note",
                            choices=["transient_retry", "bind_role", "register_interrupt", "explore_nav", "icon_template", "ask_user", "note"])
    lesson_add.add_argument("--entry", choices=list(PLATFORMS))
    lesson_add.add_argument("--package")
    lesson_add.add_argument("--step")
    lesson_add.add_argument("--activity")
    lesson_add.add_argument("--role")
    lesson_add.add_argument("--match", action="append", default=[], help="触发召回的页面关键字，可重复传参")
    lesson_add.add_argument("--source", choices=["user", "agent", "auto"], default="user")
    lesson_disable = lesson_actions.add_parser("disable", help="停用一条经验")
    lesson_disable.add_argument("lesson_id")
    lesson_disable.add_argument("--reason", default="手动停用")
    lesson_disable.add_argument("--rule-dir", dest="sub_rule_dir", type=Path, default=None)
    lesson_enable = lesson_actions.add_parser("enable", help="重新启用一条经验")
    lesson_enable.add_argument("lesson_id")
    lesson_enable.add_argument("--rule-dir", dest="sub_rule_dir", type=Path, default=None)
    lesson_fb = lesson_actions.add_parser("feedback", help="记录经验有效或失效反馈（连续3次失效自动停用）")
    lesson_fb.add_argument("lesson_id")
    lesson_fb.add_argument("--outcome", choices=["success", "failure"], required=True)
    lesson_fb.add_argument("--reason")
    lesson_fb.add_argument("--rule-dir", dest="sub_rule_dir", type=Path, default=None)
    cases = commands.add_parser("cases", help="汇总一个批次的待修复请求、失败现场和探索进度")
    cases.add_argument("run_directory", type=Path)
    teach = commands.add_parser("teach", help="当前Agent逐步驱动手机完成未知目标；每次一个已观察动作，CLI记录页面证据，resume 验收后保存路径")
    teach.add_argument("run_directory", type=Path)
    teach.add_argument("--serial")
    teach.add_argument("--adb")
    actions = teach.add_subparsers(dest="teach_action", required=True)
    for name in ("status", "observe", "launch"):
        actions.add_parser(name)
    tap = actions.add_parser("tap", help="点击一个节点、文字锚点或物理坐标（自动提取语义/模板）")
    tap.add_argument("--point", type=float, nargs=2, metavar=("X", "Y"), help="原始PNG物理坐标，自动提取语义锚点或局部模板")
    swipe = actions.add_parser("swipe", help="滑动；可选锚点作为起点")
    swipe.add_argument("--direction", choices=["up", "down"], required=True)
    swipe.add_argument("--distance", type=float, help="屏幕高度比例 0.05～0.8")
    back = actions.add_parser("back", help="系统返回（当前回答模式和药品阶段禁用）")
    for nav in (tap, swipe, back):
        nav.add_argument("--transient", action="store_true", help="一次性排障动作（如网络重试），仅推进当前现场，不写入可复用路径")
    perception = actions.add_parser("perception", help="绑定页面角色，如 --role question --node resource_id=app:id/q")
    perception.add_argument("--role", required=True)
    interrupt = actions.add_parser("interrupt", help="教一个可复用的弹层/对话框关闭规则并立即保存")
    interrupt.add_argument("--name", required=True, help="如 update_dialog")
    interrupt.add_argument("--detect-node", help="检测目标 attr=value")
    interrupt.add_argument("--detect-ocr", help="检测目标 OCR 正则")
    interrupt.add_argument("--back", action="store_true", help="用系统返回关闭，而不是点击")
    interrupt.add_argument("--entry", choices=list(PLATFORMS), help="没有当前题目时指定应用")
    teach_lesson = actions.add_parser("lesson", help="离线记录当前异常场景的一句话排障经验（默认整题验收后晋升）")
    teach_lesson.add_argument("--note", required=True, help="简短排障经验说明（2～240字）")
    teach_lesson.add_argument("--strategy", default="note",
                              choices=["transient_retry", "bind_role", "register_interrupt", "explore_nav", "icon_template", "ask_user", "note"])
    teach_lesson.add_argument("--match", action="append", default=[], help="触发召回的页面关键字，可重复传参")
    teach_lesson.add_argument("--role")
    teach_lesson.add_argument("--immediate", action="store_true", help="立即写入经验库（如登录/验证码等人工阻塞经验）")
    for command in (tap, swipe, perception, interrupt):
        command.add_argument("--node", help="attr=value，attr 取 text/description/resource_id/class_name")
        command.add_argument("--ocr", help="OCR 文字正则")
        command.add_argument("--region", type=float, nargs=4, metavar=("L", "T", "R", "B"), help="归一化区域 0～1")
        if command is not perception:
            command.add_argument("--offset", type=float, nargs=2, metavar=("X", "Y"))
    verify = commands.add_parser("verify")
    verify.add_argument("run_directory", type=Path)
    stats = commands.add_parser("stats", help="按阶段统计实际耗时，区分稳定检测、整屏采集和恢复")
    stats.add_argument("run_directory", type=Path)
    commands.add_parser("goals", help="输出内置采集目标、不可降低的验收条件和Agent接管协议")
    commands.add_parser("entries", help="列出采集入口及实际进入方式；不连接手机")
    commands.add_parser("agent-guide", help="读取随包分发的 Agent 采集和异常接管说明；不连接手机")
    setup = commands.add_parser("setup", help="为所选本地 Agent 安装或刷新随包分发的采集 Skill；不连接手机")
    setup_target = setup.add_mutually_exclusive_group(required=True)
    setup_target.add_argument("--agent", choices=["codex", "claude-code", "cursor"])
    setup_target.add_argument("--installed", action="store_true", help="刷新本机所有由 geo-collector 托管的已安装 Skill")
    setup.add_argument("--scope", choices=["user", "project"], default="user", help="默认当前用户；project 仅安装到当前工作目录")
    setup.add_argument("--dry-run", action="store_true", help="只检查目标并返回安装位置，不写文件")
    upgrade = commands.add_parser("upgrade", help="一键通过 uv 更新 CLI 并同步刷新已安装的 Agent Skill；不连接手机")
    upgrade.add_argument("--version", dest="target_version", help="升级到指定版本，如 0.1.3（默认最新）")
    upgrade.add_argument("--mirror", choices=sorted(MIRRORS), help="使用预设 PyPI 镜像（国内推荐 tuna 或 aliyun）")
    upgrade.add_argument("--index-url", help="自定义 PyPI Simple Index 地址")
    upgrade.add_argument("--no-refresh-skills", action="store_true", help="仅升级 CLI，不自动刷新已安装的 Skill")
    upgrade.add_argument("--dry-run", action="store_true", help="只输出将执行的更新命令，不实际修改环境")
    handoff = commands.add_parser("handoff", help="离线读取当前断点的精简现场，不连接手机")
    handoff.add_argument("run_directory", type=Path)
    handoff.add_argument("--limit", type=int, default=24, help="最多返回的节点候选数")
    return root


def questions(args):
    if bool(args.questions_file) == bool(args.question):
        raise ValueError("--questions-file 与 --question 必须二选一")
    lines = args.questions_file.read_text(encoding="utf-8-sig").splitlines() if args.questions_file else [args.question]
    available = {i: text.strip() for i, text in enumerate(lines, 1) if text.strip()}
    selected = list(available) if not args.question_index else list(dict.fromkeys(int(i) for i in args.question_index.split(",")))
    if not selected or any(i not in available for i in selected):
        raise ValueError("题号不存在或题目文件为空")
    return [{"index": i, "text": available[i]} for i in selected]


def new_run(args):
    from .goals import CONTRACT
    items = questions(args)
    if args.capture_current_answer and (len(items) != 1 or len(args.entry) != 1):
        raise ValueError("当前回答模式必须明确指定一个入口和一道已有问题")
    directory = args.output.resolve() / time.strftime("batch_%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    rule_root = (args.rule_dir or Path(".geo-collector")).resolve()
    teach = list(args.teach)
    if args.learn:
        teach.extend("page.perception." + entry for entry in args.entry)
        if not args.capture_current_answer:
            teach.extend("xiaohe.new_session" if entry == "xiaohe" else entry + ".open_search" for entry in args.entry)
    state = {"artifact_layout_version": 1, "goal_contract_version": CONTRACT["version"], "business_flow_version": 4,
             "status": "running", "created_at": time.time(),
             "mode": "current" if args.capture_current_answer else "batch", "serial": args.serial,
             "entries": list(dict.fromkeys(args.entry)), "questions": items, "cursor": 0,
             "rule_root": str(rule_root), "agent_requests": 0, "teach": teach,
             "tasks": [{"entry": entry, **item, "status": "pending", "phase": "start"}
                       for entry in dict.fromkeys(args.entry) for item in items]}
    write_json(directory / "调试产物" / "run.json", state)
    return directory, state


def verify_run(directory):
    from PIL import Image
    from .capture import verify_capture_images
    directory = Path(directory).resolve()
    state = read_json(directory / "调试产物" / "run.json")
    problems = []
    if state.get("status") != "complete":
        problems.append(f"批次状态为 {state.get('status')}")
    tasks = state.get("tasks", [])
    expected = {(entry, item["index"], item["text"]) for entry in state.get("entries", []) for item in state.get("questions", [])}
    actual = [(task["entry"], task["index"], task["text"]) for task in tasks]
    if not expected or set(actual) != expected or len(actual) != len(expected):
        problems.append("任务列表为空、重复或缺少所选题目")
    delivery = directory / "交付图片"
    for file in delivery.rglob("*") if delivery.exists() else []:
        if file.is_file() and file.name != ".DS_Store" and file.suffix.lower() != ".png":
            problems.append(f"交付目录出现非PNG：{file.name}")
    captured = entry_not_found = 0
    for task in state.get("tasks", []):
        prefix = f"{task['entry']}/{task['index']:03d}"
        if task.get("status") != "complete":
            problems.append(f"{prefix} 未完成")
            continue
        outcome = task.get("outcome", "captured")
        if outcome == "entry_not_found":
            from .device import parse_nodes
            from .recovery import load_observation

            entry_not_found += 1
            platform = PLATFORMS.get(task.get("entry"))
            if platform is None or not platform.REENTER_SUPPORTED:
                problems.append(f"{prefix} 当前平台不支持无入口终态")
                continue
            result_file = directory / "调试产物" / prefix / "entry-not-found.json"
            if not result_file.exists():
                problems.append(f"{prefix} 缺少entry-not-found.json")
                continue
            result = read_json(result_file)
            recorded_result = task.get("result")
            if not isinstance(recorded_result, str) or Path(recorded_result).resolve() != result_file.resolve():
                problems.append(f"{prefix} 无入口结果路径不匹配")
            if (result.get("entry"), result.get("index"), result.get("question")) != (task["entry"], task["index"], task["text"]):
                problems.append(f"{prefix} 无入口元数据题目身份不匹配")
            if (result.get("status"), result.get("outcome")) != ("complete", "entry_not_found"):
                problems.append(f"{prefix} 无入口结果不是正常终态")
            for key in ("search_query_confirmed", "search_results_confirmed", "scan_complete"):
                if result.get(key) is not True:
                    problems.append(f"{prefix} {key} 未验证")
            if result.get("platform_entry_found") is not False:
                problems.append(f"{prefix} 无入口结果与入口证据冲突")
            scope = result.get("scan_scope", {})
            if not isinstance(scope, dict):
                problems.append(f"{prefix} 入口扫描范围格式错误")
                scope = {}
            maximum, checked = scope.get("max_viewports"), scope.get("viewports_checked")
            first_screen = scope.get("type") == "first_screen"
            if not first_screen and task["entry"] != "douyin":
                problems.append(f"{prefix} 当前平台仅接受首屏入口检查")
                continue
            expected_maximum = 1 if first_screen else 5
            paths = result.get("observation_paths", [])
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                problems.append(f"{prefix} 入口扫描观察路径格式错误")
                paths = []
            if (type(maximum) is not int or maximum != expected_maximum or type(checked) is not int
                    or not 1 <= checked <= expected_maximum
                    or not 1 <= len(paths) <= expected_maximum * 2 - 1):
                problems.append(f"{prefix} 入口扫描范围证据不完整")
            scan_root = result_file.parent / "entry-scan"
            loaded = []
            for ordinal, path in enumerate(paths, 1):
                observation = Path(path).resolve()
                if (observation != (scan_root / f"{ordinal:03d}").resolve()
                        or not all((observation / name).is_file() for name in ("screen.png", "screen.xml", "observation.json"))):
                    problems.append(f"{prefix} 入口扫描观察文件缺失或路径非法")
                    continue
                try:
                    item = load_observation(observation)
                    if (observation / "screen.xml").read_bytes().decode("utf-8") != item.xml:
                        raise ValueError("层级文件与观察记录不一致")
                    recorded_nodes = item.nodes
                    item.nodes = parse_nodes(item.xml)
                    for recorded in recorded_nodes:
                        role = recorded.get("collector_role")
                        matches = [node for node in item.nodes if role
                                   and tuple(node.get("bounds", ())) == tuple(recorded.get("bounds", ()))
                                   and all(node.get(key) == recorded.get(key)
                                           for key in ("text", "description", "resource_id", "class_name", "package"))]
                        if len(matches) == 1:
                            matches[0]["collector_role"] = role
                    with Image.open(observation / "screen.png") as picture:
                        if picture.format != "PNG":
                            raise ValueError("现场截图并非PNG")
                        picture.verify()
                    if not platform.search_results_ready(item, task["text"]):
                        raise ValueError("现场不是当前完整原题的搜索结果")
                    if first_screen and not platform.results_loaded(item, task["text"]):
                        raise ValueError("首屏结果尚未加载完成，不能确认无入口")
                    if platform.brand_entry_defaults(item) or platform.platform_entry_visible(item):
                        raise ValueError("现场仍包含小荷入口证据")
                    loaded.append(item)
                except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
                    problems.append(f"{prefix} 入口扫描观察无效：{error}")
            if len(loaded) == len(paths) and loaded:
                try:
                    progress = platform.scan_progress(loaded, maximum=expected_maximum)
                    if (checked != progress["viewports_checked"]
                            or scope.get("limit_reached") is not progress["limit_reached"]
                            or scope.get("end_confirmed") is not progress["end_confirmed"]
                            or not (progress["limit_reached"] or progress["end_confirmed"])):
                        problems.append(f"{prefix} 入口扫描范围与原始画面不一致")
                except ValueError as error:
                    problems.append(f"{prefix} 入口扫描顺序无效：{error}")
            task_delivery = delivery / prefix
            if task.get("answer") or (task_delivery.exists() and any(
                    file.is_file() and file.name != ".DS_Store" for file in task_delivery.rglob("*"))):
                problems.append(f"{prefix} 无入口任务不应包含正式回答图片")
            continue
        if outcome != "captured":
            problems.append(f"{prefix} 未知业务结果 {outcome}")
            continue
        captured += 1
        result_file = directory / "调试产物" / prefix / "answer.json"
        if not result_file.exists():
            problems.append(f"{prefix} 缺少answer.json")
            continue
        result = read_json(result_file)
        if (result.get("entry"), result.get("index"), result.get("question")) != (task["entry"], task["index"], task["text"]):
            problems.append(f"{prefix} 回答元数据题目身份不匹配")
        if task["entry"] == "douyin-direct":
            from .platforms import douyin_direct
            proof = douyin_direct.submission_evidence(task, directory)
            if proof is None or result.get("direct_submission") != proof:
                problems.append(f"{prefix} 缺少与本题一致的原始发送身份证据")
        for key in ("question_confirmed", "citations_checked", "completion_confirmed", "end_confirmed", "occlusion_checked"):
            if result.get(key) is not True:
                problems.append(f"{prefix} {key} 未验证")
        for seam in result.get("seams", []):
            if seam.get("verified") is not True:
                problems.append(f"{prefix} 有未验证接缝")
        frame_count = result.get("frame_count", 0)
        if type(frame_count) is not int or frame_count < 1 or len(result.get("seams", [])) != frame_count - 1:
            problems.append(f"{prefix} 原始帧或接缝记录不完整")
        if not isinstance(result.get("citations_found"), bool) or (result.get("citations_found") and result.get("citations_expanded") is not True):
            problems.append(f"{prefix} 引用资料未证明展开")
        products = result.get("products", {})
        if not isinstance(products.get("present"), bool) or products.get("complete") is not True:
            problems.append(f"{prefix} 参考药品检查不完整")
        if products.get("present"):
            for key in ("first_item_confirmed", "end_confirmed", "images_ready", "seams_verified"):
                if products.get(key) is not True:
                    problems.append(f"{prefix} 参考药品 {key} 未验证")
        paths = result.get("image_paths", [])
        if products.get("present") and products.get("mode") == "drawer":
            count = products.get("frame_count", 0)
            product_seams = products.get("seams", [])
            if type(count) is not int or count < 1 or len(product_seams) != count - 1 or any(item.get("verified") is not True for item in product_seams):
                problems.append(f"{prefix} 药品原始帧或接缝记录不完整")
            if not products.get("image_paths") or not set(products["image_paths"]).issubset(paths):
                problems.append(f"{prefix} 药品交付图片缺失")
        if not paths:
            problems.append(f"{prefix} 没有交付图片")
        for path in paths:
            image_path = Path(path).resolve()
            if not image_path.is_relative_to(delivery / prefix) or image_path.suffix != ".png":
                problems.append(f"{prefix} 正式图片路径不合法")
                continue
            try:
                with Image.open(image_path) as picture:
                    if picture.format != "PNG":
                        raise ValueError("文件并非PNG")
                    picture.verify()
            except (OSError, ValueError):
                problems.append(f"{prefix} PNG不可读")
        try:
            verify_capture_images(result, result_file.parent, delivery / prefix)
        except (OSError, TypeError, ValueError) as error:
            problems.append(f"{prefix} 原始帧与交付图片验收失败：{error}")
    return {"complete": not problems, "tasks": len(state.get("tasks", [])),
            "captured": captured, "entry_not_found": entry_not_found,
            "agent_requests": state.get("agent_requests", 0), "recovery_actions": len(state.get("recovery_actions", [])),
            "goal_exploration_steps": state.get("goal_exploration_steps", 0), "goal_route_replay_steps": state.get("goal_route_replay_steps", 0),
            "problems": problems, "run_directory": str(directory)}


def execute(args):
    if args.command == "agent-guide":
        from .setup import agent_guide
        return {"format": "markdown", "guide": agent_guide()}
    if args.command == "setup":
        from .setup import install_skill, refresh_installed_skills
        if args.installed:
            return refresh_installed_skills(scope=args.scope, dry_run=args.dry_run)
        return install_skill(args.agent, scope=args.scope, dry_run=args.dry_run)
    if args.command == "upgrade":
        from .setup import upgrade_cli
        return upgrade_cli(target_version=args.target_version, index_url=args.index_url, mirror=args.mirror,
                           dry_run=args.dry_run, refresh_skills=not args.no_refresh_skills)
    if args.command == "entries":
        from .platforms import ENTRY_LABELS
        return {"entries": [{"id": key, "name": ENTRY_LABELS[key], "package": platform.PACKAGE,
                             "default_selected": key in {"xiaohe", "douyin"}}
                            for key, platform in PLATFORMS.items()]}
    if args.command == "handoff":
        from .handoff import handoff_summary
        return handoff_summary(args.run_directory, limit=args.limit)
    if args.command == "goals":
        from .goals import CONTRACT, ROLES
        return {**CONTRACT, "roles": sorted(ROLES),
                "explore_example": {"actions": [{"type": "tap", "target": {"kind": "node", "attr": "description", "value": "更多"}}], "explore": True},
                "perception_example": {"perception": {"question": {"kind": "node", "attr": "resource_id", "value": "example:id/question"}}}}
    if args.command == "stats":
        directory = args.run_directory.resolve()
        state = read_json(directory / "调试产物" / "run.json")
        values = {}
        with (directory / "调试产物" / "events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if isinstance(row.get("seconds"), (int, float)):
                    values.setdefault(row["event"], []).append(row["seconds"])
        result = {"timings": {key: {"samples": len(items), "median_seconds": round(statistics.median(items), 3)} for key, items in values.items()},
                  "agent_requests": state.get("agent_requests", 0), "recovery_actions": len(state.get("recovery_actions", []))}
        ledger = directory / "调试产物" / "supervisor.json"
        if ledger.exists():
            from .supervisor import usage_summary
            result["supervisor"] = usage_summary(read_json(ledger).get("calls", []))
        return result
    if args.command == "repair":
        return submit_repair(args.request, point=args.point, plan=read_json(args.plan) if args.plan else None,
                             sha256=args.sha256, candidate=args.candidate, role=args.role, preview=args.preview)
    if args.command == "rules":
        return {"rules": RuleStore(args.rule_dir).health()}
    if args.command == "lessons":
        from .lessons import LessonStore
        root = getattr(args, "sub_rule_dir", None) or args.rule_dir or Path(".geo-collector")
        store = LessonStore(root)
        sub = args.lesson_action or "list"
        if sub == "list":
            include_all = bool(args.include_inactive or getattr(args, "sub_include_inactive", False))
            return {"lessons": store.list(include_inactive=include_all), "file": str(store.file)}
        if sub == "add":
            saved = store.add({
                "entry": args.entry or "",
                "package": args.package or "",
                "step": args.step or "",
                "activity": args.activity or "",
                "match_texts": args.match,
                "strategy": args.strategy,
                "role": args.role,
                "note": args.note,
                "source": args.source,
            })
            return {"lesson": saved, "file": str(store.file)}
        if sub == "disable":
            return {"lesson": store.disable(args.lesson_id, reason=args.reason)}
        if sub == "enable":
            return {"lesson": store.enable(args.lesson_id)}
        if sub == "feedback":
            updated = store.record_outcome(args.lesson_id, success=args.outcome == "success", reason=args.reason)
            if updated is None:
                raise ValueError(f"未找到经验记录：{args.lesson_id}")
            return {"lesson": updated}
        raise ValueError(f"未知经验操作 {sub}")
    if args.command == "teach" and args.teach_action == "lesson":
        from .teach import record_run_lesson
        return record_run_lesson(args.run_directory, note=args.note, strategy=args.strategy,
                                 match_texts=args.match, role=args.role, immediate=args.immediate)
    if args.command == "cases":
        from .teach import list_cases
        return list_cases(args.run_directory)
    if args.command == "verify":
        return verify_run(args.run_directory)
    agent_limits = None
    if getattr(args, "agent", "none") == "codex":
        if sys.platform == "win32":
            raise ValueError("独立 Codex 观察器支持 macOS/Linux。Windows 请用普通 run/resume，由当前对话 Agent 接管。")
        from .supervisor import SupervisorLimits
        agent_limits = SupervisorLimits(max_calls=args.max_agent_calls,
                                        max_calls_per_task=args.max_agent_calls_per_task,
                                        timeout=args.agent_timeout)
        if args.repair == "wait":
            raise ValueError("--agent 已处理异常接管，请使用默认 --repair pause")
    from .device import Device
    directory, state = None, None
    if args.command == "run":
        directory, state = new_run(args)
    elif args.command in {"resume", "recover", "teach", "supervise"}:
        directory = args.run_directory.resolve()
        state = read_json(directory / "调试产物" / "run.json")
        if args.serial and state.get("serial") and args.serial != state["serial"]:
            raise ValueError("不能在另一台手机恢复未完成的会话，请新建批次")
        if getattr(args, "rule_dir", None) and args.rule_dir.resolve() != Path(state["rule_root"]):
            raise ValueError("恢复必须使用原规则库")
    rule_root = Path(state["rule_root"]) if state else (args.rule_dir or Path(".geo-collector")).resolve()
    # ponytail: one collector per computer, including different rule roots; add per-device locks and SQLite for parallel phones.
    with DeviceLease(Path.home() / ".cache" / "geo-collector", "phone-control"):
        device = Device(serial=args.serial or (state or {}).get("serial"), adb=args.adb)
        try:
            if args.command in {"doctor", "inspect"}:
                observation = device.observe(ocr=True)
                save_observation(args.output.resolve(), observation)
                return {"serial": device.serial, "package": observation.package, "activity": observation.activity,
                        "logical_size": observation.logical_size, "physical_size": observation.physical_size,
                        "nodes": len(observation.nodes), "ocr_lines": len(observation.texts),
                        "sha256": hashlib.sha256(observation.png).hexdigest(), "observation": str(args.output.resolve())}
            if args.command == "teach":
                from .teach import teach
                if state.get("serial") and state["serial"] != device.serial:
                    raise ValueError("不能在另一台手机上教学当前批次")
                return teach(device, RuleStore(rule_root), directory, args)
            if args.wait_timeout < 0:
                raise ValueError("wait-timeout不能小于0")
            from .workflow import run_questions
            state["serial"] = device.serial
            write_json(directory / "调试产物" / "run.json", state)
            print(json.dumps({"event": "run_opened", "run_directory": str(directory)}, ensure_ascii=False), file=sys.stderr, flush=True)
            if args.command == "recover":
                from .workflow import recover_current
                recover_current(device, RuleStore(rule_root), directory, state, args.action, args.reason)
            store = RuleStore(rule_root)

            def collect():
                run_questions(device, store, directory, state,
                              wait=args.wait_timeout if args.repair == "wait" else 0,
                              teach=sorted(set(args.teach) | set(state.get("teach", []))))
                return verify_run(directory)

            if agent_limits is not None:
                from .agent_runtime import supervise_collection
                return supervise_collection(device, store, directory, state, resume=collect, limits=agent_limits,
                                            retry_failed=args.retry_agent_error)
            return collect()
        finally:
            device.close()


def main(argv=None):
    if sys.platform == "win32":
        # Redirected streams otherwise use the system code page on Windows.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        with event_output(getattr(args, "events", False)):
            result = execute(args)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        if result.get("status") == "needs_repair":
            return 20
        return 1 if result.get("complete") is False else 0
    except NeedsRepair as error:
        file = Path(error.request)
        handoff = {"status": "needs_repair", "request": error.request, "exit_code": 20}
        if file.is_file():
            request = read_json(file)
            if request.get("run_directory") and request.get("sha256"):
                handoff.update(next=["handoff", request["run_directory"]],
                               preview=["repair", str(file), "--sha256", request["sha256"], "--preview"],
                               finish=["verify", request["run_directory"]])
        print(json.dumps(handoff, ensure_ascii=False), flush=True)
        return 20
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
