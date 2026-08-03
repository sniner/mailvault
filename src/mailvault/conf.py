"""Loading the TOML configuration into `Config`, `JobConfig` and `CopyConfig`.

Parses the `[global]` options, the `[[job]]` list and the `[copy]` section,
expands `${VAR}` and `_cmd` values (the latter only with `--allow-exec`), and
drops fields that were retired in an earlier version with a warning rather than
silently restoring a default.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
import subprocess
import tomllib

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


# Fields that no longer exist, and what to say about each. An unknown field is
# only warned about and dropped, which for a boolean would silently restore a
# default -- someone who deliberately turned something off would find it back on.
# These are therefore named explicitly.
RETIRED_FIELDS = {
    "with_db": "metadata is always recorded now, and there is no database to have",
    "with_metadata": "metadata is always recorded now",
    "incremental": "it is a global option now -- set it once under [global], not per job",
    "role": "the [copy] section names its 'source' and 'destination' jobs itself now",
    "archive_folder": (
        "it is '[copy] move_to_folder' now; 'archive' means the local archive, "
        "and this is a folder on the source server"
    ),
    "move_to_archive": (
        "naming a '[copy] move_to_folder' is what turns moving on now, "
        "so the separate switch is gone"
    ),
}


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
        resolved = cls._drop_retired_fields(name, resolved)
        fields = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in resolved.items() if k in fields}
        unknown = set(resolved.keys()) - fields
        if unknown:
            log.warning("Unknown config fields in '%s': %s", name, ", ".join(sorted(unknown)))
        return cls(name=name, **known)

    @staticmethod
    def _drop_retired_fields(name: str, data: dict) -> dict:
        """Report fields that no longer exist, rather than ignoring them quietly.

        A dropped field is otherwise indistinguishable from a typo, and a reader
        of the configuration would go on believing it still does something.
        """
        remaining = dict(data)
        for field, reason in RETIRED_FIELDS.items():
            if field in remaining:
                remaining.pop(field)
                log.warning("%s: '%s' no longer exists -- %s", name, field, reason)
        return remaining


@dataclasses.dataclass
class CopyConfig:
    """The `[copy]` section -- everything the `copy` command needs, in one place.

    `source` and `destination` name two `[[job]]` entries rather than tagging
    them. A job then says only how to reach a mailbox, and nothing in the job
    list has to know that a command exists which never touches an archive.
    """

    source: str = ""
    destination: str = ""
    move_to_folder: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> CopyConfig:
        fields = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - fields
        if unknown:
            log.warning("Unknown fields in [copy]: %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in data.items() if k in fields})

    def resolve(self, jobs: list[JobConfig]) -> tuple[JobConfig, JobConfig]:
        """Look the two named jobs up in `jobs`.

        Raises ConfigError naming what is wrong -- an unset name, a name with no
        matching job, or both roles on the same one. A mistyped job name is
        caught here rather than surfacing later as a copy that finds nothing, and
        the same job on both ends would copy a mailbox onto itself.
        """
        by_name: dict[str, JobConfig] = {}
        for job in jobs:
            by_name.setdefault(job.name, job)

        found = []
        for role in ("source", "destination"):
            name = getattr(self, role)
            if not name:
                raise ConfigError(f"[copy]: '{role}' is not set")
            job = by_name.get(name)
            if job is None:
                known = ", ".join(sorted(by_name)) or "none defined"
                raise ConfigError(f"[copy]: {role} job {name!r} does not exist (jobs: {known})")
            found.append(job)

        source, destination = found
        if source is destination:
            raise ConfigError(
                f"[copy]: source and destination are the same job ({source.name!r})"
            )
        return source, destination


# Config fields that come from their own part of the file rather than from the
# `[global]` table, and must not be accepted as global options.
_NON_GLOBAL_FIELDS = frozenset({"jobs", "copy"})


@dataclasses.dataclass
class Config:
    jobs: list[JobConfig] = dataclasses.field(default_factory=list)
    copy: CopyConfig | None = None
    compress: bool = False
    index_db: bool = False
    incremental: bool = True

    @classmethod
    def from_toml(cls, data: dict, allow_exec: bool = False) -> Config:
        global_data = data.get("global", {})
        fields = {f.name for f in dataclasses.fields(cls) if f.name not in _NON_GLOBAL_FIELDS}
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

        copy_data = data.get("copy")
        copy = CopyConfig.from_dict(copy_data) if copy_data is not None else None

        return cls(jobs=jobs, copy=copy, **known_global)


def load(path: pathlib.Path | str, allow_exec: bool = False) -> Config:
    """Load a TOML configuration file.

    The file name does not matter -- the content is always parsed as TOML.
    """
    path = pathlib.Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read configuration: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not a valid TOML configuration: {exc}") from exc
    return Config.from_toml(data, allow_exec=allow_exec)
