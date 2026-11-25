"""Skill installation must work offline and preserve existing user instructions."""
import hashlib
import json
from pathlib import Path

import pytest

from geo_collector import cli, device, setup


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "user home 用户"
    project = tmp_path / "data workspace"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(device, "Device", lambda **_: pytest.fail("setup must not access the phone"))
    return home, project


@pytest.mark.parametrize("agent,folder,canonical", [
    ("codex", ".agents", "codex"),
    ("claude-code", ".claude", "claude-code"),
    ("cursor", ".cursor", "cursor"),
    ("antigravity", ".gemini", "antigravity"),
    ("gemini", ".gemini", "antigravity"),
])
@pytest.mark.parametrize("scope", ["user", "project"])
def test_setup_installs_complete_guide_in_selected_scope_only(workspace, agent, folder, canonical, scope):
    home, project = workspace
    root, other = (home, project) if scope == "user" else (project, home)
    result = cli.execute(cli.parser().parse_args(["setup", "--agent", agent, "--scope", scope]))
    skill = root / folder / "skills/geo-collector/SKILL.md"
    ref = root / folder / "skills/geo-collector/references/install.md"
    assert result["status"] == "installed" and result["agent"] == canonical and result["skill_path"] == str(skill)
    assert skill.read_text(encoding="utf-8") == setup.agent_guide()
    assert ref.read_text(encoding="utf-8") == setup.install_reference()
    assert not list(other.iterdir())
    assert not (project / "captures").exists()
    assert not (project / ".geo-collector").exists()


def test_dry_run_returns_destination_without_writes(workspace):
    home, project = workspace
    result = setup.install_skill("codex", dry_run=True)
    assert result["status"] == "preview" and result["action"] == "installed"
    assert not list(home.iterdir()) and not list(project.iterdir())
    guide = setup.agent_guide()
    assert "启动前范围确认" in guide and "xiaohe-geo-collector" in guide


def test_repeat_install_is_idempotent_and_upgrade_preserves_extra_files(workspace, monkeypatch):
    first = setup.install_skill("codex")
    skill = Path(first["skill_path"])
    original = skill.read_bytes()
    modified = skill.stat().st_mtime_ns
    extra = skill.parent / "personal-notes.md"
    extra.write_text("keep me", encoding="utf-8")
    assert setup.install_skill("codex")["status"] == "unchanged"
    assert skill.stat().st_mtime_ns == modified
    updated = original.decode() + "\nNew bundled guidance.\n"
    monkeypatch.setattr(setup, "agent_guide", lambda: updated)
    assert setup.install_skill("codex", dry_run=True)["action"] == "updated"
    assert skill.read_bytes() == original
    assert setup.install_skill("codex")["status"] == "updated"
    assert skill.read_text(encoding="utf-8") == updated
    assert extra.read_text() == "keep me"
    receipt = json.loads((skill.parent / setup.RECEIPT).read_text())
    assert receipt["sha256"] == hashlib.sha256(skill.read_bytes()).hexdigest()


@pytest.mark.parametrize("receipt", [None, "broken json", "null", "[]", '{"owner":"other"}'])
def test_unmanaged_skill_is_never_overwritten(workspace, receipt):
    home, _ = workspace
    skill = home / ".agents/skills/geo-collector/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("personal instructions", encoding="utf-8")
    if receipt is not None:
        (skill.parent / setup.RECEIPT).write_text(receipt, encoding="utf-8")
    with pytest.raises(ValueError, match="已保留原文件"):
        setup.install_skill("codex")
    assert skill.read_text() == "personal instructions"


def test_edited_managed_skill_is_preserved(workspace):
    installed = setup.install_skill("codex")
    skill = Path(installed["skill_path"])
    skill.write_text("user edited this", encoding="utf-8")
    with pytest.raises(ValueError, match="已保留原文件"):
        setup.install_skill("codex")
    assert skill.read_text() == "user edited this"


def test_identical_existing_skill_is_not_claimed_by_installer(workspace):
    home, _ = workspace
    skill = home / ".agents/skills/geo-collector/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(setup.agent_guide(), encoding="utf-8")
    assert setup.install_skill("codex")["status"] == "unchanged"
    assert not (skill.parent / setup.RECEIPT).exists()


def test_managed_symlink_is_not_followed(workspace):
    home, project = workspace
    target = project / "managed-skill"
    target.mkdir()
    skill = target / "SKILL.md"
    skill.write_text("managed by another tool", encoding="utf-8")
    link = home / ".agents/skills/geo-collector"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable for this user")
    with pytest.raises(ValueError, match="原管理工具"):
        setup.install_skill("codex")
    assert skill.read_text() == "managed by another tool"


def test_claude_custom_user_config_does_not_affect_project_scope(workspace, monkeypatch):
    home, project = workspace
    config = home / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    user = setup.install_skill("claude-code")
    local = setup.install_skill("claude-code", scope="project")
    assert Path(user["skill_path"]) == config / "skills/geo-collector/SKILL.md"
    assert Path(local["skill_path"]) == project / ".claude/skills/geo-collector/SKILL.md"


def test_setup_cli_returns_failure_for_conflict(workspace, capsys):
    setup.install_skill("cursor")
    home, _ = workspace
    skill = home / ".cursor/skills/geo-collector/SKILL.md"
    skill.write_text("edited", encoding="utf-8")
    assert cli.main(["setup", "--agent", "cursor"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_setup_installed_refreshes_only_managed_skills_and_skips_edited(workspace, monkeypatch):
    setup.install_skill("codex")
    setup.install_skill("antigravity")
    setup.install_skill("cursor")
    home, _ = workspace
    (home / ".cursor/skills/geo-collector/SKILL.md").write_text("custom edit", encoding="utf-8")
    monkeypatch.setattr(setup, "agent_guide", lambda: "updated guide content\n")
    monkeypatch.setattr(setup, "install_reference", lambda: "updated install ref\n")

    result = cli.execute(cli.parser().parse_args(["setup", "--installed"]))
    assert result["status"] == "updated"
    assert [row["agent"] for row in result["skills"]] == ["codex", "antigravity"]
    assert all(row["action"] == "updated" for row in result["skills"])
    assert (home / ".agents/skills/geo-collector/SKILL.md").read_text(encoding="utf-8") == "updated guide content\n"
    assert (home / ".gemini/skills/geo-collector/references/install.md").read_text(encoding="utf-8") == "updated install ref\n"
    assert [row["agent"] for row in result["skipped"]] == ["cursor"]
    assert not (home / ".claude").exists()


def test_legacy_receipt_upgrades_with_references_and_edited_reference_is_preserved(workspace):
    home, _ = workspace
    directory = home / ".agents/skills/geo-collector"
    directory.mkdir(parents=True)
    skill = directory / "SKILL.md"
    old_guide = "legacy skill content\n"
    skill.write_text(old_guide, encoding="utf-8")
    (directory / setup.RECEIPT).write_text(json.dumps({
        "owner": "geo-collector",
        "sha256": hashlib.sha256(old_guide.encode("utf-8")).hexdigest(),
        "version": "0.1.3",
    }), encoding="utf-8")

    upgraded = setup.install_skill("codex")
    ref = directory / "references/install.md"
    assert upgraded["status"] == "updated"
    assert skill.read_text(encoding="utf-8") == setup.agent_guide()
    assert ref.read_text(encoding="utf-8") == setup.install_reference()

    # Editing the bundled reference protects it from overwrite.
    ref.write_text("custom install notes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="已保留原文件"):
        setup.install_skill("codex")
    assert ref.read_text(encoding="utf-8") == "custom install notes\n"


def test_upgrade_dry_run_and_mirror_and_version_validation(workspace, monkeypatch):
    preview = cli.execute(cli.parser().parse_args(["upgrade", "--mirror", "tuna", "--dry-run"]))
    assert preview["status"] == "preview"
    assert preview["index_url"] == setup.MIRRORS["tuna"]
    assert "--default-index" in preview["commands"][0] and "upgrade" in preview["commands"][0]
    assert preview["commands"][1] == ["geo-collect", "setup", "--installed"]

    pinned = cli.execute(cli.parser().parse_args(["upgrade", "--version", "0.2.0", "--no-refresh-skills", "--dry-run"]))
    assert pinned["commands"] == [[pinned["commands"][0][0], "tool", "install", "--force", "--python", "3.11", "xiaohe-geo-collector==0.2.0"]]

    with pytest.raises(ValueError, match="版本号格式无效"):
        cli.execute(cli.parser().parse_args(["upgrade", "--version", "bad;rm", "--dry-run"]))
    with pytest.raises(ValueError, match="只能提供一个"):
        cli.execute(cli.parser().parse_args(["upgrade", "--mirror", "tuna", "--index-url", "https://example.com/simple", "--dry-run"]))

    calls = []
    class Proc:
        def __init__(self, stdout="", returncode=0):
            self.stdout, self.stderr, self.returncode = stdout, "", returncode

    def fake_run(cmd, **_):
        calls.append(cmd)
        if cmd[1:4] == ["tool", "dir", "--bin"]:
            return Proc(str(bin_dir) + "\n")
        if "--version" in cmd:
            return Proc("geo-collect 0.2.0\n")
        if "--installed" in cmd:
            return Proc(json.dumps({"status": "updated", "skills": []}))
        return Proc()

    bin_dir = workspace[0] / "uv-bin"
    bin_dir.mkdir()
    tool_geo = bin_dir / "geo-collect"
    tool_geo.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/venv/bin/{name}")
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    upgraded = cli.execute(cli.parser().parse_args(["upgrade", "--mirror", "aliyun"]))
    assert upgraded["status"] == "upgraded" and upgraded["version"] == "0.2.0"
    assert calls[0] == ["/venv/bin/uv", "tool", "upgrade", "--default-index", setup.MIRRORS["aliyun"], "xiaohe-geo-collector"]
    assert calls[1] == ["/venv/bin/uv", "tool", "dir", "--bin"]
    assert calls[2] == [str(tool_geo), "setup", "--installed"]
    assert calls[3] == [str(tool_geo), "--version"]


def test_setup_custom_dir_installs_and_updates_without_agent_allowlist(workspace, monkeypatch):
    home, _ = workspace
    custom_root = home / "any-agent" / "skills"
    first = cli.execute(cli.parser().parse_args(["setup", "--dir", str(custom_root)]))
    skill = custom_root / "geo-collector" / "SKILL.md"
    ref = custom_root / "geo-collector" / "references" / "install.md"
    assert first["status"] == "installed" and first["agent"] == "custom" and first["scope"] == "custom"
    assert first["skill_path"] == str(skill)
    assert skill.read_text(encoding="utf-8") == setup.agent_guide()
    assert ref.read_text(encoding="utf-8") == setup.install_reference()

    # Passing the geo-collector folder directly does not nest geo-collector/geo-collector.
    direct = cli.execute(cli.parser().parse_args(["setup", "--dir", str(custom_root / "geo-collector")]))
    assert direct["status"] == "unchanged" and direct["skill_path"] == str(skill)
    assert not (custom_root / "geo-collector" / "geo-collector").exists()

    # Managed update works and user edits remain protected.
    monkeypatch.setattr(setup, "agent_guide", lambda: "updated custom guide\n")
    updated = setup.install_skill(target_dir=custom_root)
    assert updated["status"] == "updated" and skill.read_text(encoding="utf-8") == "updated custom guide\n"
    skill.write_text("user customized", encoding="utf-8")
    with pytest.raises(ValueError, match="已保留原文件"):
        setup.install_skill(target_dir=custom_root)
    with pytest.raises(ValueError, match="只能提供一个"):
        setup.install_skill("codex", target_dir=custom_root)
