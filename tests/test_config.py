import pytest
from pathlib import Path
import yaml

from src.gitreport.config import load_config, Config

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

def test_load_config_validation_error(tmp_path: Path):
    """Test that ValueError is raised for a schema validation error."""
    config_content = {"providers": {"github": {"username": "testuser"}}}  # Missing token
    config_file = tmp_path / "invalid_schema.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_content, f)

    with pytest.raises(ValueError, match="Configuration validation error"):
        load_config(config_file)
