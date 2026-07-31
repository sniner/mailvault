from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
import subprocess
import tomllib

import yaml

log = logging.getLogger(__name__)

VALID_BACKENDS = ("imap", "msgraph")

# Fields each backend cannot work without. Validated per job so a misconfigured
# mailbox fails early with a clear message instead of deep inside the backend.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "imap": ("server", "username"),
    "msgraph": ("tenant_id", "client_id", "client_secret", "username"),
}


class ConfigError(Exception):
    pass


def _expand_env(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} patterns in a string."""

    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        if ":-" in var:
            name, default = var.split(":-", 1)
            return os.environ.get(name) or default
        return os.environ.get(var) or m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _resolve_values(data: dict, allow_exec: bool = False) -> dict:
    """Expand environment variables in string values and resolve *_cmd fields."""
    resolved = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = _expand_env(value)
        resolved[key] = value

    cmd_keys = [k for k in resolved if k.endswith("_cmd")]
    for cmd_key in cmd_keys:
        target_key = cmd_key[:-4]
        cmd = resolved.pop(cmd_key)
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        if not allow_exec:
            log.warning("Ignoring '%s' (use --allow-exec to enable command execution)", cmd_key)
            continue
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.error(
                    "Command '%s' failed (exit %d): %s",
                    cmd,
                    result.returncode,
                    result.stderr.strip(),
                )
                continue
            resolved[target_key] = result.stdout.strip()
        except subprocess.TimeoutExpired:
            log.error("Command '%s' timed out", cmd)
        except OSError as exc:
            log.error("Command '%s' failed: %s", cmd, exc)
    return resolved


@dataclasses.dataclass
class JobConfig:
    name: str = "."
    server: str = "localhost"
    port: int = 993
    username: str = ""
    password: str = ""
    tls: bool = True
    tls_check_hostname: bool = True
    tls_verify_cert: bool = True
    folders: list[str] | None = None
    ignore_folder_flags: list[str] = dataclasses.field(default_factory=list)
    ignore_folder_names: list[str] = dataclasses.field(default_factory=list)
    delete_after_export: bool = False
    exchange_journal: bool = False
    trash_folder: str | None = None
    error_folder: str | None = None
    with_db: bool = True
    incremental: bool = True
    role: str | None = None
    move_to_archive: bool = False
    archive_folder: str | None = None
    backend: str = "imap"
    max_retries: int = 5
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    def validate(self) -> None:
        """Check that the backend is known and its required fields are present.

        Raises ConfigError with a message naming the offending job, so a typo in
        `backend` or a missing Graph credential fails early and clearly instead
        of silently falling back to IMAP or crashing deep in the backend.
        """
        if self.backend not in VALID_BACKENDS:
            raise ConfigError(
                f"{self.name}: unknown backend {self.backend!r} "
                f"(expected one of {', '.join(VALID_BACKENDS)})"
            )
        missing = [f for f in _REQUIRED_FIELDS[self.backend] if not getattr(self, f)]
        if missing:
            raise ConfigError(
                f"{self.name}: backend {self.backend!r} requires: {', '.join(missing)}"
            )

    @classmethod
    def from_dict(cls, name: str, data: dict, allow_exec: bool = False) -> JobConfig:
        resolved = _resolve_values(data, allow_exec=allow_exec)
        fields = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in resolved.items() if k in fields}
        unknown = set(resolved.keys()) - fields
        if unknown:
            log.warning("Unknown config fields in '%s': %s", name, ", ".join(sorted(unknown)))
        return cls(name=name, **known)


@dataclasses.dataclass
class Config:
    jobs: list[JobConfig] = dataclasses.field(default_factory=list)
    compress: bool = False

    @classmethod
    def from_toml(cls, data: dict, allow_exec: bool = False) -> Config:
        global_data = data.get("global", {})
        fields = {f.name for f in dataclasses.fields(cls) if f.name != "jobs"}
        known_global = {k: v for k, v in global_data.items() if k in fields}
        unknown_global = set(global_data.keys()) - fields
        if unknown_global:
            log.warning("Unknown global config fields: %s", ", ".join(sorted(unknown_global)))

        jobs = []
        for job_data in data.get("job", []):
            name = job_data.get("name", ".")
            jobs.append(
                JobConfig.from_dict(
                    name,
                    {k: v for k, v in job_data.items() if k != "name"},
                    allow_exec=allow_exec,
                )
            )

        return cls(jobs=jobs, **known_global)

    @classmethod
    def from_yaml(cls, data: dict, allow_exec: bool = False) -> Config:
        # The legacy YAML format is a flat mapping of job-name -> job-config; it
        # has no `global` section, so global options like `compress` are only
        # available via TOML (see from_toml). Kept for backwards compatibility.
        jobs = [
            JobConfig.from_dict(name, content, allow_exec=allow_exec)
            for name, content in data.items()
        ]
        return cls(jobs=jobs)


def load(path: pathlib.Path | str, allow_exec: bool = False) -> Config:
    path = pathlib.Path(path)
    suffix = path.suffix.lower()

    if suffix == ".toml":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return Config.from_toml(data, allow_exec=allow_exec)
    else:
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f)
        return Config.from_yaml(data, allow_exec=allow_exec)


def find(configs: list[JobConfig], key: str, value: str) -> JobConfig | None:
    _value = value.casefold()
    return next(
        (c for c in configs if (getattr(c, key, "") or "").casefold() == _value),
        None,
    )
