import os
import re
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
        # S19: digest_formats is the closed set {html, md}. "json" in
        # particular is forbidden: the .json model snapshot is renderer
        # infrastructure (always written, never symlinked). `md` is required
        # because `gitreport read` re-renders the markdown digests. Only
        # non-path-dependent rules live here: the digest/state collision
        # check runs in load_config after relative paths are anchored (see
        # _validate_path_collisions).
        if "md" not in self.digest_formats:
            raise ValueError("`gitreport read` requires the `md` digest format")
        unknown = sorted(set(self.digest_formats) - {"html", "md"})
        if unknown:
            raise ValueError(
                f"digest_formats only supports html and md (got: {', '.join(unknown)})"
            )
        return self


class Config(BaseModel):
    """Root model for the application's configuration."""

    providers: dict[str, ProviderConfig]
    attention: AttentionConfig = Field(default_factory=AttentionConfig)


# CFG-01: digest artifacts are written as `<stem>.<ext>` beside the stem, so
# an artifact path resolving onto the state store would let a digest run
# overwrite it. The YYYY-MM-DD token in a digest stem stands for any concrete
# date, so the dated variant is matched by pattern, not by a fixed substitute.
_DIGEST_EXTENSIONS = ("json", "md", "html")
_DATE_TOKEN = "YYYY-MM-DD"
_DATE_NAME_PATTERN = r"\d{4}-\d{2}-\d{2}"


def _validate_path_collisions(config: Config, config_path: Path) -> None:
    """Reject digest artifact paths that resolve onto state_path (S19/CFG-01).

    Runs after load_config has anchored still-relative attention paths to the
    config file's parent, so the comparison sees the paths a digest run would
    actually use — a pre-anchoring check cannot catch a relative digest stem
    whose anchor collides with a ~-absolute state_path. digest_latest is
    checked too, since its artifacts are refreshed on every digest run.

    Args:
        config: The validated configuration (paths already anchored).
        config_path: The config file the paths were anchored against
            (cited in the error message for context).
    """
    attention = config.attention
    state = attention.state_path.expanduser().resolve(strict=False)

    for label in ("digest_output", "digest_latest"):
        stem = getattr(attention, label).expanduser()
        for ext in _DIGEST_EXTENSIONS:
            artifact = stem.with_name(stem.name + "." + ext)
            # Plain (token-free) stem: exact resolved-path equality — both
            # sides fully resolved (expanduser + resolve), so symlinks and
            # `..` segments cannot dodge the check.
            collides = artifact.resolve(strict=False) == state
            # Dated stem: the token stands for any date, so the artifact's
            # file name matches state_path's file name by pattern
            # (2026-09-28.json — but not state.json), with the resolved
            # parent directories required to be equal as well.
            if not collides and _DATE_TOKEN in artifact.name:
                pattern = re.escape(artifact.name).replace(
                    re.escape(_DATE_TOKEN), _DATE_NAME_PATTERN
                )
                collides = bool(re.fullmatch(pattern, state.name)) and (
                    stem.parent.resolve(strict=False) == state.parent
                )
            if collides:
                raise ValueError(
                    f"digest/state path collision: {label}'s .{ext} artifact "
                    f"resolves to state_path ({config_path})"
                )


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
    # R9b: a still-relative attention path is anchored at the config file's
    # parent (expanduser first, so ~-paths stay absolute and win), never at
    # whatever CWD the command happened to run from.
    base = path.parent
    for field in ("state_path", "digest_output", "digest_latest"):
        value: Path = getattr(config.attention, field).expanduser()
        if not value.is_absolute():
            value = base / value
        setattr(config.attention, field, value)
    # CFG-01: the digest/state collision check runs only now that every
    # attention path is anchored, so it compares what a digest run would
    # actually write (see _validate_path_collisions).
    _validate_path_collisions(config, path)
    return config
