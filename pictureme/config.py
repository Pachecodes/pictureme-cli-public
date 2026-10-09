"""Persistent CLI configuration.

Holds the active host + API key on disk so users don't have to re-auth on
every invocation. Lives in a per-user config dir (~/.config/pictureme on
Linux, %APPDATA%/pictureme on Windows, etc.) so we don't collide with the
project repo.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_config_dir

CONFIG_APP = "pictureme"
DEFAULT_HOST = "https://go.pictureme.now"

ENV_HOST = "PICTUREME_HOST"
ENV_API_KEY = "PICTUREME_API_KEY"


@dataclass
class CLIConfig:
    host: str = DEFAULT_HOST
    api_key: Optional[str] = None
    # Future: profiles, default model, default aspect ratio.
    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.host = normalize_host(self.host)


def normalize_host(host: str) -> str:
    """Return the API origin regardless of a pasted API prefix."""
    if not isinstance(host, str):
        raise ValueError("host must be an absolute HTTP(S) URL")
    parsed = urlsplit(host.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("host must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("host URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("host URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if path not in {"", "/api", "/api/v3"}:
        raise ValueError("host URL path must be empty, /api, or /api/v3")
    hostname = parsed.hostname or ""
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("host URL contains an invalid port") from exc
    if parsed.scheme.lower() == "http" and not _is_loopback_host(hostname):
        raise ValueError("PictureME bearer tokens require HTTPS for remote hosts")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def config_path() -> Path:
    return Path(user_config_dir(CONFIG_APP)) / "config.json"


def load_config() -> CLIConfig:
    path = config_path()
    if not path.exists():
        return CLIConfig()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return CLIConfig()
    if not isinstance(data, dict):
        return CLIConfig()
    try:
        return CLIConfig(
            host=data.get("host", DEFAULT_HOST),
            api_key=data.get("api_key"),
            extras=data.get("extras", {}),
        )
    except (TypeError, ValueError):
        # Never retarget a stored bearer token when its bound host is invalid.
        return CLIConfig()


def save_config(cfg: CLIConfig) -> Path:
    path = config_path()
    if os.name != "posix" and cfg.api_key:
        raise OSError("Persistent credentials require POSIX permissions; use PICTUREME_API_KEY instead")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Write privately from creation, then atomically replace (never follow a file symlink).
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            stream.write(json.dumps(asdict(cfg), indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd != -1:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def resolve_runtime(
    cli_host: Optional[str] = None,
    cli_api_key: Optional[str] = None,
) -> CLIConfig:
    """Layered resolution: CLI flag > env var > stored config > defaults.

    `cli_host` / `cli_api_key` come from the top-level --host / --api-key
    options. Env vars are checked next, then the saved config file.
    """
    cfg = load_config()
    host = cli_host or os.environ.get(ENV_HOST) or cfg.host or DEFAULT_HOST
    api_key = cli_api_key or os.environ.get(ENV_API_KEY)
    if not api_key and normalize_host(host) == cfg.host:
        api_key = cfg.api_key
    return CLIConfig(host=host.rstrip("/"), api_key=api_key, extras=cfg.extras)
