"""Install the bundled collection skill for a user's chosen local Agent."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile


AGENT_DIRS = {"codex": ".agents", "claude-code": ".claude", "cursor": ".cursor"}
RECEIPT = ".geo-collector-install.json"


def agent_guide() -> str:
    guide = files("geo_collector").joinpath("agent_guide.md")
    if not guide.is_file():
        guide = Path(__file__).resolve().parents[2] / ".agents/skills/geo-collector/SKILL.md"
    return guide.read_text(encoding="utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install_skill(agent: str, *, scope: str = "user", dry_run: bool = False) -> dict:
    if agent not in AGENT_DIRS or scope not in {"user", "project"}:
        raise ValueError("请选择支持的 Agent 和 user/project 安装范围")
    base = Path.home() if scope == "user" else Path.cwd()
    # Claude Code supports a custom configuration root; project skills remain local.
    config = (Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
              if agent == "claude-code" and scope == "user" and os.environ.get("CLAUDE_CONFIG_DIR")
              else base / AGENT_DIRS[agent])
    directory = config.absolute() / "skills" / "geo-collector"
    skill = directory / "SKILL.md"
    receipt = directory / RECEIPT
    # A shared/manager-owned deployment must be updated through its manager.
    if any(path.is_symlink() for path in (skill, receipt, directory, config / "skills", config)):
        raise ValueError(f"Skill 路径包含符号链接，请通过原管理工具更新：{directory}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"Skill 安装路径不是目录：{directory}")
    content = agent_guide().encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    action = "installed"
    if skill.exists():
        existing = skill.read_bytes()
        if existing == content:
            action = "unchanged"
        else:
            try:
                saved = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
            managed = (isinstance(saved, dict) and saved.get("owner") == "geo-collector"
                       and saved.get("sha256") == hashlib.sha256(existing).hexdigest())
            if not managed:
                raise ValueError(f"已有同名 Skill 未由本安装器管理或已被修改，已保留原文件：{skill}")
            action = "updated"
    result = {
        "status": "preview" if dry_run else action,
        "action": action, "agent": agent, "scope": scope, "skill_path": str(skill),
        "version": version("geo-collector"),
        "next": "在数据工作目录重新打开 Agent 会话，使用 geo-collector Skill 采集你的问题。",
        "prompt": "使用 geo-collector 采集以下问题，先检查手机连接；遇到异常自行检查、修复并恢复原批次，验收后交付图片和逐题结果。",
    }
    if dry_run or action == "unchanged":
        return result
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(skill, content)
    _atomic_write(receipt, json.dumps({"owner": "geo-collector", "sha256": digest,
                                      "version": result["version"]}, ensure_ascii=False).encode("utf-8"))
    return result
