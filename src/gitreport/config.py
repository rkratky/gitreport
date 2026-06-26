import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

# --- Pydantic Models for Configuration ---

class ProviderConfig(BaseModel):
    """Base model for a provider's configuration."""
    username: str
    token: str = Field(..., description="API token for authentication")

class Config(BaseModel):
    """Root model for the application's configuration."""
    providers: Dict[str, ProviderConfig]

# --- Configuration Loading ---

DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.config/gitreport/config.yaml"))

def load_config(config_path: Optional[Path] = None) -> Config:
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
        raise FileNotFoundError(
            f"Configuration file not found. Please create one at: {path}"
        )

    with open(path, "r") as f:
        try:
            config_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing YAML file: {e}")

    if not config_data:
        raise ValueError("Configuration file is empty.")

    try:
        return Config.model_validate(config_data)
    except ValidationError as e:
        raise ValueError(f"Configuration validation error: {e}")
