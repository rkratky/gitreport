from pathlib import Path

import pytest
import yaml
from gitreport.config import Config, load_config


def test_load_config_success(tmp_path: Path):
    """Test that a valid config file is loaded correctly."""
    config_content = {
        "providers": {
            "github": {"username": "testuser", "token": "gh-token"},
            "launchpad": {"username": "lp-user", "token": "lp-token"},
        }
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_content, f)

    config = load_config(config_file)
    assert isinstance(config, Config)
    assert "github" in config.providers
    assert config.providers["github"].username == "testuser"
    assert "launchpad" in config.providers


def test_load_config_file_not_found():
    """Test that FileNotFoundError is raised for a missing file."""
    with pytest.raises(FileNotFoundError):
        load_config(Path("non_existent_file.yaml"))


def test_load_config_empty_file(tmp_path: Path):
    """Test that ValueError is raised for an empty file."""
    config_file = tmp_path / "empty.yaml"
    config_file.touch()
    with pytest.raises(ValueError, match="Configuration file is empty."):
        load_config(config_file)


def test_load_config_invalid_yaml(tmp_path: Path):
    """Test that ValueError is raised for invalid YAML."""
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text("providers: [github,")
    with pytest.raises(ValueError, match="Error parsing YAML file"):
        load_config(config_file)


def test_load_config_token_optional(tmp_path: Path):
    """A provider may omit the token (e.g. Launchpad uses external auth)."""
    config_content = {
        "providers": {
            "launchpad": {"username": "lp-user"},  # No token.
        }
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_content, f)

    config = load_config(config_file)
    assert config.providers["launchpad"].username == "lp-user"
    assert config.providers["launchpad"].token is None


def test_load_config_validation_error(tmp_path: Path):
    """Test that ValueError is raised for a schema validation error."""
    config_content = {"providers": {"github": {"token": "gh-token"}}}  # Missing username
    config_file = tmp_path / "invalid_schema.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_content, f)

    with pytest.raises(ValueError, match="Configuration validation error"):
        load_config(config_file)


def test_attention_block_optional_with_defaults(tmp_path: Path):
    """An existing config without `attention:` validates and gets defaults."""
    config_content = {"providers": {"github": {"username": "u", "token": "t"}}}
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)
    assert config.attention.stale_pr_days == 7
    assert config.attention.digest_formats == ["html", "md"]
    assert config.attention.exclusions == {}
    assert str(config.attention.state_path).endswith("state.json")


def test_attention_custom_values(tmp_path: Path):
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "exclusions": {"github": ["me/fork-*"]},
            "stale_pr_days": 3,
            "state_path": "~/tmp/state.json",
            "digest_formats": ["html", "md"],
            "digest_output": "~/tmp/digests/YYYY-MM-DD",
            "digest_latest": "~/tmp/digests/latest",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)
    assert config.attention.stale_pr_days == 3
    assert config.attention.exclusions == {"github": ["me/fork-*"]}
    assert not str(config.attention.state_path).startswith("~")


def test_attention_md_format_required(tmp_path: Path):
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {"digest_formats": ["html"]},
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="requires the .md. digest format"):
        load_config(config_file)
