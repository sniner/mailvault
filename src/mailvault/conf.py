"""Loading the TOML configuration into `Config` and `JobConfig`.

Parses the `[global]` options and the `[[job]]` list, expands `${VAR}` and
`_cmd` values (the latter only with `--allow-exec`), and reports fields and
sections that were retired in an earlier version rather than silently ignoring
them or restoring a default.
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


# What the `copy` command left behind. It was removed in 0.9.0, so these say
# that it is gone rather than pointing at a replacement -- there is none in this
# tool, and a config carrying them was written for something that no longer runs.
_COPY_IS_GONE = "the 'copy' command was removed in 0.9.0; use imapsync or mbsync instead"

# Fields that no longer exist, and what to say about each. An unknown field is
# only warned about and dropped, which for a boolean would silently restore a
# default -- someone who deliberately turned something off would find it back on.
# These are therefore named explicitly.
RETIRED_FIELDS = {
    "with_db": "metadata is always recorded now, and there is no database to have",
    "with_metadata": "metadata is always recorded now",
    "incremental": "it is a global option now -- set it once under [global], not per job",
    "role": _COPY_IS_GONE,
    "archive_folder": _COPY_IS_GONE,
    "move_to_archive": _COPY_IS_GONE,
}

# Whole sections that no longer do anything, reported for the same reason.
RETIRED_SECTIONS = {"copy": _COPY_IS_GONE}


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
    # Each of these belongs to exactly one backend: deleting really deletes on
    # plain IMAP, but Gmail moves the message to a trash folder whose name is
    # localised (so only the owner knows it), and Graph soft-deletes into
    # Deleted Items. See `validate`.
    trash_folder: str | None = None
    permanent_delete: bool = False
    error_folder: str | None = None
    backend: str = "imap"
    max_retries: int = 5
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    def validate(self) -> None:
        """Check that the backend is known and the options make sense together.

        Raises ConfigError with a message naming the offending job, so a typo in
        `backend` or a missing Graph credential fails early and clearly instead
        of silently falling back to IMAP or crashing deep in the backend.

        An option that does nothing in its context is warned about; one that
        would do something destructive nobody asked for stops the job.
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
        # `trash_folder` and `permanent_delete` are the same idea for the two
        # hosted providers that only pretend to delete, one backend each. Both
        # are refused rather than ignored where they cannot work: an option that
        # decides the fate of mail must never look effective while doing
        # nothing. Neither means anything without `delete_after_export` -- with
        # nothing being deleted there is nothing left to finish off.
        if self.trash_folder:
            if self.backend != "imap":
                raise ConfigError(
                    f"{self.name}: 'trash_folder' is an IMAP option (Gmail) and has no effect "
                    f"on backend {self.backend!r} -- use 'permanent_delete' instead"
                )
            if not self.delete_after_export:
                raise ConfigError(
                    f"{self.name}: 'trash_folder' empties that folder completely and only "
                    f"makes sense together with 'delete_after_export' -- set that too, or "
                    f"remove it"
                )
        if self.permanent_delete:
            if self.backend != "msgraph":
                raise ConfigError(
                    f"{self.name}: 'permanent_delete' is an msgraph option and has no effect "
                    f"on backend {self.backend!r}"
                    + (" -- use 'trash_folder' instead" if self.backend == "imap" else "")
                )
            if not self.delete_after_export:
                raise ConfigError(
                    f"{self.name}: 'permanent_delete' only makes sense together with "
                    f"'delete_after_export' -- set that too, or remove it"
                )
        if self.error_folder and not self.exchange_journal:
            # The error folder is the escape hatch for the one case that can go
            # wrong on its own: an item in a journal mailbox that is not a
            # journal envelope. An ordinary backup only reads and, on request,
            # deletes -- it never relocates, so there is nothing to catch. It is
            # inert rather than harmful here, hence a warning and not an error.
            log.warning(
                "%s: 'error_folder' only applies to 'exchange_journal' jobs "
                "and does nothing here",
                self.name,
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


def _archive_path(value: object, base_dir: pathlib.Path | None) -> pathlib.Path:
    """Turn a configured `destination` into a path.

    `~` and `${VAR}` are expanded, and a relative path is taken relative to the
    configuration file rather than the working directory: a configuration names
    one particular archive, and which one that is must not depend on where the
    command happened to be started from -- least of all from cron.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("[global] destination must be a non-empty path")
    path = pathlib.Path(_expand_env(value)).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path


@dataclasses.dataclass
class Config:
    jobs: list[JobConfig] = dataclasses.field(default_factory=list)
    compress: bool = False
    index_db: bool = False
    incremental: bool = True
    # The archive this configuration belongs to, if it says. Optional: the
    # command line still names one, and naming it in both places is allowed --
    # see `mailvault.cli.commands.archive_path` for which one wins.
    destination: pathlib.Path | None = None

    @classmethod
    def from_toml(
        cls,
        data: dict,
        allow_exec: bool = False,
        base_dir: pathlib.Path | None = None,
    ) -> Config:
        if "copy" in data:
            log.warning("[copy] no longer does anything -- %s", RETIRED_SECTIONS["copy"])

        global_data = data.get("global", {})
        fields = {f.name for f in dataclasses.fields(cls) if f.name != "jobs"}
        known_global = {k: v for k, v in global_data.items() if k in fields}
        unknown_global = set(global_data.keys()) - fields
        if unknown_global:
            log.warning("Unknown global config fields: %s", ", ".join(sorted(unknown_global)))

        # TOML gives a string where the dataclass wants a path, and the expansion
        # needs the file's directory, which the other options have no use for.
        if "destination" in known_global:
            known_global["destination"] = _archive_path(known_global["destination"], base_dir)

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
    return Config.from_toml(data, allow_exec=allow_exec, base_dir=path.parent)
