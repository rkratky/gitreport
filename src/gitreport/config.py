import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

# --- Pydantic Models for Configuration ---


class ProviderConfig(BaseModel):
    """Base model for a provider's configuration."""

    username: str
    token: SecretStr | None = Field(
        default=None,
        description=(
            "API token for authentication. Required by providers that use "
            "token auth (e.g. GitHub); may be omitted for providers that "
            "authenticate out-of-band (e.g. Launchpad's saved OAuth "
            "credentials)."
        ),
    )


class AttentionConfig(BaseModel):
    """Configuration for the attention digest feature."""

    exclusions: dict[str, list[str]] = Field(default_factory=dict)
    stale_pr_days: int = 7
    state_path: Path = Path("~/.local/state/gitreport/state.json")
    digest_formats: list[str] = Field(default_factory=lambda: ["html", "md"])
    digest_output: Path = Path("~/.local/state/gitreport/digests/YYYY-MM-DD")
    digest_latest: Path = Path("~/.local/state/gitreport/digests/latest")

    @model_validator(mode="after")
    def _validate_formats(self) -> "AttentionConfig":
        if "md" not in self.digest_formats:
            raise ValueError("`gitreport read` requires the `md` digest format")
        return self


class Config(BaseModel):
    """Root model for the application's configuration."""

    providers: dict[str, ProviderConfig]
    attention: AttentionConfig = Field(default_factory=AttentionConfig)


# --- Configuration Loading ---

DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.config/gitreport/config.yaml"))


def load_config(config_path: Path | None = None) -> Config:
    """
    Loads, validates, and returns the application configuration.

    Args:
        config_path: The path to the configuration file. If None, the default
                     path (~/.config/gitreport/config.yaml) is used.

    Returns:
        A validated Config object.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        ValueError: If the configuration file is invalid.
    """
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found. Please create one at: {path}")

    with open(path) as f:
        try:
            config_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing YAML file: {e}")

    if config_data is None:
        raise ValueError("Configuration file is empty.")

    try:
        config = Config.model_validate(config_data)
    except ValidationError as e:
        raise ValueError(f"Configuration validation error: {e}")
    config.attention.state_path = config.attention.state_path.expanduser()
    config.attention.digest_output = config.attention.digest_output.expanduser()
    config.attention.digest_latest = config.attention.digest_latest.expanduser()
    return config
