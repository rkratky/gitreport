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
        # S19: digest_formats is the closed set {html, md}. "json" in
        # particular is forbidden: the .json model snapshot is renderer
        # infrastructure (always written, never symlinked).
        unknown = sorted(set(self.digest_formats) - {"html", "md"})
        if unknown:
            raise ValueError(
                f"digest_formats only supports html and md (got: {', '.join(unknown)})"
            )
        # S19/CFG-01: a digest artifact path resolving onto the state store
        # would let a digest run overwrite it — reject the collision. Both
        # sides are compared fully resolved (expanduser + resolve), so
        # symlinks and `..` segments cannot dodge the check; the YYYY-MM-DD
        # token is compared both raw and substituted (a literal dated
        # state_path would otherwise slip past the raw form); digest_latest
        # is checked too, since its artifacts are refreshed on every digest
        # run. (The validator runs before load_config anchors still-relative
        # paths, so relative configs resolve against the CWD on both sides —
        # equality is anchor-independent when both sides share a base, and a
        # ~-expanded absolute path can never equal a bare relative one.)
        state = self.state_path.expanduser().resolve(strict=False)

        def _collision_ext(stem: Path) -> str | None:
            candidates = [stem]
            if "YYYY-MM-DD" in stem.name:
                candidates.append(stem.with_name(stem.name.replace("YYYY-MM-DD", "1970-01-01")))
            for candidate in candidates:
                for ext in ("json", "md", "html"):
                    artifact = candidate.with_name(candidate.name + "." + ext)
                    if artifact.expanduser().resolve(strict=False) == state:
                        return ext
            return None

        for label, stem in (
            ("digest_output", self.digest_output.expanduser()),
            ("digest_latest", self.digest_latest.expanduser()),
        ):
            ext = _collision_ext(stem)
            if ext is not None:
                raise ValueError(
                    f"digest/state path collision: {label}'s .{ext} artifact resolves to state_path"
                )
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
    # R9b: a still-relative attention path is anchored at the config file's
    # parent (expanduser first, so ~-paths stay absolute and win), never at
    # whatever CWD the command happened to run from.
    base = path.parent
    for field in ("state_path", "digest_output", "digest_latest"):
        value: Path = getattr(config.attention, field).expanduser()
        if not value.is_absolute():
            value = base / value
        setattr(config.attention, field, value)
    return config
