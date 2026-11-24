"""Install the bundled collection skill for a user's chosen local Agent."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


AGENT_DIRS = {"codex": ".agents", "claude-code": ".claude", "cursor": ".cursor"}
RECEIPT = ".geo-collector-install.json"
PACKAGE_NAME = "xiaohe-geo-collector"
MIRRORS = {
    "pypi": "https://pypi.org/simple",
    "tuna": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
}


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.0.0-dev"


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
        "version": package_version(),
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


def _skill_dir(agent: str, scope: str) -> Path:
    base = Path.home() if scope == "user" else Path.cwd()
    config = (Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
              if agent == "claude-code" and scope == "user" and os.environ.get("CLAUDE_CONFIG_DIR")
              else base / AGENT_DIRS[agent])
    return config.absolute() / "skills" / "geo-collector"


def refresh_installed_skills(*, scope: str = "user", dry_run: bool = False) -> dict:
    """Refresh every geo-collector managed skill already installed in this scope."""
    if scope not in {"user", "project"}:
        raise ValueError("scope 仅支持 user/project")
    updated, skipped = [], []
    for agent in AGENT_DIRS:
        directory = _skill_dir(agent, scope)
        receipt = directory / RECEIPT
        skill = directory / "SKILL.md"
        if not receipt.is_file() or not skill.is_file() or receipt.is_symlink() or skill.is_symlink():
            continue
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"agent": agent, "skill_path": str(skill), "reason": "安装凭据无法解析，已保留原文件"})
            continue
        if not isinstance(saved, dict) or saved.get("owner") != "geo-collector":
            continue
        try:
            updated.append(install_skill(agent, scope=scope, dry_run=dry_run))
        except ValueError as error:
            skipped.append({"agent": agent, "skill_path": str(skill), "reason": str(error)})
    return {
        "status": "preview" if dry_run else ("updated" if updated else "none_installed"),
        "scope": scope,
        "version": package_version(),
        "skills": updated,
        "skipped": skipped,
    }


def upgrade_cli(*, target_version: str | None = None, index_url: str | None = None,
                mirror: str | None = None, dry_run: bool = False, refresh_skills: bool = True) -> dict:
    """Upgrade the globally installed uv tool and refresh managed Agent skills."""
    if index_url and mirror:
        raise ValueError("--index-url 和 --mirror 只能提供一个")
    if mirror:
        if mirror not in MIRRORS:
            raise ValueError(f"未知镜像 {mirror}；可选：{sorted(MIRRORS)}")
        index_url = MIRRORS[mirror]
    if target_version and not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", target_version):
        raise ValueError(f"版本号格式无效：{target_version}")
    uv = shutil.which("uv")
    if not uv and not dry_run:
        raise RuntimeError("未找到 uv；请先安装 uv 或按 INSTALL.md 更新")
    uv_bin = uv or "uv"
    if target_version:
        cmd = [uv_bin, "tool", "install", "--force", "--python", "3.11"]
        if index_url:
            cmd.extend(["--default-index", index_url])
        cmd.append(f"{PACKAGE_NAME}=={target_version}")
    else:
        cmd = [uv_bin, "tool", "upgrade"]
        if index_url:
            cmd.extend(["--default-index", index_url])
        cmd.append(PACKAGE_NAME)
    skill_cmd = ["geo-collect", "setup", "--installed"]
    before_version = package_version()
    if dry_run:
        return {
            "status": "preview",
            "current_version": before_version,
            "target_version": target_version or "latest",
            "index_url": index_url,
            "commands": [cmd, *( [skill_cmd] if refresh_skills else [] )],
        }
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"uv 更新失败（退出码 {proc.returncode}）：{detail}")
    skill_result = None
    if refresh_skills:
        geo_bin = shutil.which("geo-collect")
        if geo_bin:
            refreshed = subprocess.run([geo_bin, "setup", "--installed"], capture_output=True, text=True)
            if refreshed.returncode == 0 and refreshed.stdout.strip():
                try:
                    skill_result = json.loads(refreshed.stdout)
                except ValueError:
                    skill_result = {"raw": refreshed.stdout.strip()[:240]}
            else:
                skill_result = {"warning": (refreshed.stderr or refreshed.stdout or "未能自动刷新 Skill").strip()[:240]}
    new_version = before_version
    geo_bin = shutil.which("geo-collect")
    if geo_bin:
        ver_proc = subprocess.run([geo_bin, "--version"], capture_output=True, text=True)
        if ver_proc.returncode == 0 and ver_proc.stdout.strip():
            new_version = ver_proc.stdout.strip().split()[-1]
    return {
        "status": "upgraded",
        "previous_version": before_version,
        "version": new_version,
        "index_url": index_url,
        "skills": skill_result,
    }
