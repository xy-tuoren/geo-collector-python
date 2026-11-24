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


AGENT_DIRS = {
    "codex": ".agents",
    "claude-code": ".claude",
    "cursor": ".cursor",
    "antigravity": ".gemini",
}
AGENT_ALIASES = {"gemini": "antigravity"}
SUPPORTED_AGENTS = tuple(AGENT_DIRS) + tuple(AGENT_ALIASES)
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


def install_reference() -> str:
    ref = files("geo_collector").joinpath("skill_references/install.md")
    if not ref.is_file():
        ref = Path(__file__).resolve().parents[2] / ".agents/skills/geo-collector/references/install.md"
    return ref.read_text(encoding="utf-8")


def _bundled_skill_files() -> dict[str, bytes]:
    return {
        "SKILL.md": agent_guide().encode("utf-8"),
        "references/install.md": install_reference().encode("utf-8"),
    }


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _normalize_agent(agent: str) -> str:
    return AGENT_ALIASES.get(agent, agent)


def _skill_dir(agent: str, scope: str) -> Path:
    canonical = _normalize_agent(agent)
    base = Path.home() if scope == "user" else Path.cwd()
    # Claude Code supports a custom configuration root; project skills remain local.
    config = (Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
              if canonical == "claude-code" and scope == "user" and os.environ.get("CLAUDE_CONFIG_DIR")
              else base / AGENT_DIRS[canonical])
    return config.absolute() / "skills" / "geo-collector"


def _check_path_safety(directory: Path, relpaths: tuple[str, ...]) -> None:
    config = directory.parent.parent
    check_paths = [config, directory.parent, directory, directory / RECEIPT]
    for rel in relpaths:
        target = directory / rel
        parent = target.parent
        while parent != directory and parent != parent.parent:
            check_paths.append(parent)
            parent = parent.parent
        check_paths.append(target)
    if any(path.is_symlink() for path in check_paths):
        raise ValueError(f"Skill 路径包含符号链接，请通过原管理工具更新：{directory}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"Skill 安装路径不是目录：{directory}")
    for rel in relpaths:
        target = directory / rel
        parent = target.parent
        while parent != directory and parent != parent.parent:
            if parent.exists() and not parent.is_dir():
                raise ValueError(f"Skill 附属路径不是目录：{parent}")
            parent = parent.parent
        if target.exists() and target.is_dir():
            raise ValueError(f"Skill 目标路径是目录而非文件：{target}")


def install_skill(agent: str, *, scope: str = "user", dry_run: bool = False) -> dict:
    canonical = _normalize_agent(agent)
    if canonical not in AGENT_DIRS or scope not in {"user", "project"}:
        raise ValueError("请选择支持的 Agent 和 user/project 安装范围")
    directory = _skill_dir(canonical, scope)
    skill = directory / "SKILL.md"
    receipt = directory / RECEIPT
    bundle = _bundled_skill_files()
    _check_path_safety(directory, tuple(bundle))

    saved = None
    if receipt.exists():
        try:
            parsed = json.loads(receipt.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and parsed.get("owner") == "geo-collector":
                saved = parsed
        except (OSError, ValueError):
            saved = None

    saved_files = saved.get("files") if isinstance(saved, dict) and isinstance(saved.get("files"), dict) else {}
    content = bundle["SKILL.md"]
    digest = hashlib.sha256(content).hexdigest()
    managed = False
    if skill.exists():
        existing_skill = skill.read_bytes()
        skill_hash = hashlib.sha256(existing_skill).hexdigest()
        managed = saved is not None and saved.get("sha256") == skill_hash
        if not managed:
            # Preserve identical unmanaged SKILL.md without claiming ownership.
            if existing_skill == content:
                ref_conflict = any((directory / rel).exists() and (directory / rel).read_bytes() != data
                                   for rel, data in bundle.items() if rel != "SKILL.md")
                if not ref_conflict:
                    action = "unchanged"
                else:
                    raise ValueError(f"已有同名 Skill 附属文件未由本安装器管理或已被修改，已保留原文件：{directory}")
            else:
                raise ValueError(f"已有同名 Skill 未由本安装器管理或已被修改，已保留原文件：{skill}")
        else:
            for rel, data in bundle.items():
                if rel == "SKILL.md":
                    continue
                target = directory / rel
                if target.exists():
                    existing_extra = target.read_bytes()
                    if existing_extra != data:
                        expected_hash = saved_files.get(rel)
                        if expected_hash != hashlib.sha256(existing_extra).hexdigest():
                            raise ValueError(f"已有同名 Skill 附属文件未由本安装器管理或已被修改，已保留原文件：{target}")
            all_match = all((directory / rel).is_file() and (directory / rel).read_bytes() == data
                            for rel, data in bundle.items())
            action = "unchanged" if all_match else "updated"
    else:
        for rel, data in bundle.items():
            if rel == "SKILL.md":
                continue
            target = directory / rel
            if target.exists() and target.read_bytes() != data:
                raise ValueError(f"已有同名 Skill 附属文件未由本安装器管理或已被修改，已保留原文件：{target}")
        action = "installed"

    result = {
        "status": "preview" if dry_run else action,
        "action": action, "agent": canonical, "scope": scope, "skill_path": str(skill),
        "files": [str(directory / rel) for rel in bundle],
        "version": package_version(),
        "next": "在数据工作目录重新打开 Agent 会话，使用 geo-collector Skill 采集你的问题。",
        "prompt": "使用 geo-collector 采集以下问题，先检查手机连接；遇到异常自行检查、修复并恢复原批次，验收后交付图片和逐题结果。",
    }
    if dry_run or action == "unchanged":
        return result
    directory.mkdir(parents=True, exist_ok=True)
    file_hashes = {}
    for rel, data in bundle.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            _atomic_write(target, data)
        file_hashes[rel] = hashlib.sha256(data).hexdigest()
    _atomic_write(receipt, json.dumps({
        "owner": "geo-collector",
        "sha256": digest,
        "files": file_hashes,
        "version": result["version"],
    }, ensure_ascii=False).encode("utf-8"))
    return result


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


def _resolve_uv_tool_geo_collect(uv_bin: str) -> str | None:
    try:
        proc = subprocess.run([uv_bin, "tool", "dir", "--bin"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            bin_dir = Path(proc.stdout.strip().splitlines()[-1].strip())
            for name in ("geo-collect.exe", "geo-collect") if os.name == "nt" else ("geo-collect",):
                candidate = bin_dir / name
                if candidate.is_file():
                    return str(candidate)
    except (OSError, subprocess.SubprocessError):
        pass
    return shutil.which("geo-collect")


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
    geo_bin = _resolve_uv_tool_geo_collect(uv_bin)
    skill_result = None
    if refresh_skills and geo_bin:
        refreshed = subprocess.run([geo_bin, "setup", "--installed"], capture_output=True, text=True)
        if refreshed.returncode == 0 and refreshed.stdout.strip():
            try:
                skill_result = json.loads(refreshed.stdout)
            except ValueError:
                skill_result = {"raw": refreshed.stdout.strip()[:240]}
        else:
            skill_result = {"warning": (refreshed.stderr or refreshed.stdout or "未能自动刷新 Skill").strip()[:240]}
    new_version = before_version
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
