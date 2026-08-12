from pathlib import Path

import pytest

from src.config import AiMaintainerConfig, kill_switch_engaged, load_config

REPO_CONFIG = Path(__file__).parent.parent / ".github" / "ai-maintainer.yaml"


def test_loads_real_repo_config():
    config = load_config(REPO_CONFIG)
    assert config.enabled is True
    assert config.workflows["dependabot_auto_merge"] is True
    assert config.dependabot.auto_merge == "patch_and_minor"
    assert "bug" in config.issue_triage.labels.values()


def test_rejects_bad_auto_merge_value():
    with pytest.raises(ValueError):
        AiMaintainerConfig.model_validate({"dependabot": {"auto_merge": "yolo"}})


def test_missing_file_falls_back_to_safe_defaults(tmp_path):
    config = load_config(tmp_path / "does-not-exist.yaml")
    assert config.workflow_enabled("dependabot_auto_merge") is False


def test_workflow_enabled_requires_both_switches():
    config = AiMaintainerConfig(enabled=True, workflows={"dependabot_auto_merge": False})
    assert config.workflow_enabled("dependabot_auto_merge") is False
    config = AiMaintainerConfig(enabled=False, workflows={"dependabot_auto_merge": True})
    assert config.workflow_enabled("dependabot_auto_merge") is False
    config = AiMaintainerConfig(enabled=True, workflows={"dependabot_auto_merge": True})
    assert config.workflow_enabled("dependabot_auto_merge") is True


def test_kill_switch_repo_variable_wins_even_if_config_enabled():
    config = AiMaintainerConfig(enabled=True)
    assert kill_switch_engaged(config, lambda name: "false") is True
    assert kill_switch_engaged(config, lambda name: "true") is False
    assert kill_switch_engaged(config, lambda name: None) is False


def test_kill_switch_config_disabled_even_if_repo_variable_missing():
    config = AiMaintainerConfig(enabled=False)
    assert kill_switch_engaged(config, lambda name: None) is True
