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


@pytest.mark.parametrize("agent,folder", [("codex", ".agents"), ("claude-code", ".claude"), ("cursor", ".cursor")])
@pytest.mark.parametrize("scope", ["user", "project"])
def test_setup_installs_complete_guide_in_selected_scope_only(workspace, agent, folder, scope):
    home, project = workspace
    root, other = (home, project) if scope == "user" else (project, home)
    result = cli.execute(cli.parser().parse_args(["setup", "--agent", agent, "--scope", scope]))
    skill = root / folder / "skills/geo-collector/SKILL.md"
    assert result["status"] == "installed" and result["skill_path"] == str(skill)
    assert skill.read_text(encoding="utf-8") == setup.agent_guide()
    assert not list(other.iterdir())
    assert not (project / "captures").exists()
    assert not (project / ".geo-collector").exists()


def test_dry_run_returns_destination_without_writes(workspace):
    home, project = workspace
    result = setup.install_skill("codex", dry_run=True)
    assert result["status"] == "preview" and result["action"] == "installed"
    assert not list(home.iterdir()) and not list(project.iterdir())


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
