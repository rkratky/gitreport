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


def test_relative_attention_paths_resolve_against_config_dir(tmp_path: Path):
    """R9b: a still-relative attention path resolves against the config
    file's parent (after expanduser), not the process CWD."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "state/state.json",
            "digest_output": "digests/YYYY-MM-DD",
            "digest_latest": "digests/latest",
        },
    }
    config_file = tmp_path / "sub" / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)

    assert config.attention.state_path == tmp_path / "sub" / "state" / "state.json"
    assert config.attention.digest_output == tmp_path / "sub" / "digests" / "YYYY-MM-DD"
    assert config.attention.digest_latest == tmp_path / "sub" / "digests" / "latest"


def test_attention_md_format_required(tmp_path: Path):
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {"digest_formats": ["html"]},
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="requires the .md. digest format"):
        load_config(config_file)


def test_digest_formats_reject_unknown_value(tmp_path: Path):
    """S19: digest_formats is a closed set {html, md} — e.g. `json` is
    forbidden (the snapshot has no symlink and is not user-configurable)."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {"digest_formats": ["html", "md", "json"]},
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="digest_formats"):
        load_config(config_file)


@pytest.mark.parametrize("ext", ["json", "md", "html"])
def test_digest_output_state_path_collision_rejected(tmp_path: Path, ext: str):
    """S19: a digest artifact path that resolves onto state_path would let a
    digest run overwrite the state store — the config must be rejected."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": f"digests/YYYY-MM-DD.{ext}",
            "digest_output": "digests/YYYY-MM-DD",
            "digest_latest": "digests/latest",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="collision"):
        load_config(config_file)


def test_digest_output_state_path_no_collision(tmp_path: Path):
    """The default-ish layout (state.json next to dated digest stems) is fine."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "state/state.json",
            "digest_output": "digests/YYYY-MM-DD",
            "digest_latest": "digests/latest",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)

    assert config.attention.state_path == tmp_path / "state" / "state.json"


def test_digest_output_stem_state_collision_rejected(tmp_path: Path):
    """CFG-01: a digest_output stem named `state` puts every digest artifact
    (.json/.md/.html) on the state store — rejected (hardened resolved-path
    check still catches the plain case)."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "digests/state.json",
            "digest_output": "digests/state",
            "digest_latest": "digests/latest",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="collision"):
        load_config(config_file)


def test_digest_latest_stem_state_collision_rejected(tmp_path: Path):
    """CFG-01: digest_latest's artifacts are refreshed on every digest run —
    a `latest` stem colliding with state_path is rejected (the original check
    only compared digest_output)."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "digests/state.json",
            "digest_output": "digests/YYYY-MM-DD",
            "digest_latest": "digests/state",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="collision"):
        load_config(config_file)


def test_digest_output_dated_state_path_collision_rejected(tmp_path: Path):
    """CFG-01-R1: a literal dated state file name (e.g. 2026-09-28.json)
    collides with the YYYY-MM-DD digest stem by pattern — not only via a
    fixed substitute date."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": str(tmp_path / "digests" / "2026-09-28.json"),
            "digest_output": str(tmp_path / "digests" / "YYYY-MM-DD"),
            "digest_latest": str(tmp_path / "digests" / "latest"),
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="collision"):
        load_config(config_file)


def test_digest_output_token_stem_plain_state_path_accepted(tmp_path: Path):
    r"""CFG-01-R1: the dated pattern only matches \d{4}-\d{2}-\d{2} file
    names — `state.json` next to the YYYY-MM-DD stem does not collide."""
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": str(tmp_path / "digests" / "state.json"),
            "digest_output": str(tmp_path / "digests" / "YYYY-MM-DD"),
            "digest_latest": str(tmp_path / "digests" / "latest"),
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)

    assert config.attention.state_path == tmp_path / "digests" / "state.json"


def test_collision_check_runs_after_anchoring(tmp_path: Path, monkeypatch):
    """CFG-01-R2: the collision check must see post-anchoring paths. A
    relative `digest_output` anchored to the config file's parent colliding
    with a ~-absolute `state_path` is rejected (a pre-anchoring check would
    compare against the CWD and miss it)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".config" / "gitreport"
    config_dir.mkdir(parents=True)
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "~/.config/gitreport/state.json",
            "digest_output": "state",  # relative → anchored next to the config
            "digest_latest": "digests/latest",
        },
    }
    config_file = config_dir / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="collision"):
        load_config(config_file)


def test_no_collision_after_anchoring_accepted(tmp_path: Path, monkeypatch):
    """CFG-01-R2: non-colliding relative paths still load after anchoring."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".config" / "gitreport"
    config_dir.mkdir(parents=True)
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "state_path": "~/.config/gitreport/state.json",
            "digest_output": "digests/YYYY-MM-DD",
            "digest_latest": "digests/latest",
        },
    }
    config_file = config_dir / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)

    assert config.attention.state_path == (tmp_path / ".config" / "gitreport" / "state.json")
    assert config.attention.digest_output == (
        tmp_path / ".config" / "gitreport" / "digests" / "YYYY-MM-DD"
    )
