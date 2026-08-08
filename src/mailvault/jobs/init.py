"""Make a directory into an archive, the way `git init` makes one into a repository.

Everything else in the program asks `marker.is_archive` first and refuses a
directory that is not one. This is where an archive comes from: the three
directories it is made of, the mark that says which layout they are written in,
and a configuration to fill in.

The mark is written **last**, for the same reason the migration writes it last:
an interrupted run leaves a directory that is not an archive yet, rather than one
that claims to be an archive and is missing half of itself.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib

from mailvault.jobs.common import JobError
from mailvault.store import cas, heads, marker, metalog

log = logging.getLogger(__name__)

# The configuration an archive carries, as a starting point. Enough to see the
# shape and to fill in, not enough to run -- a file that ran as it stands would
# be a file somebody forgot to look at.
CONFIG_TEMPLATE = """\
# The archive this file lies in describes itself here. Every command reads it,
# so `cd` into the archive and `mailvault backup` needs nothing else.
#
# Full reference: https://github.com/sniner/mailvault#configuration

[global]
# compress = true      # store the messages zstd-compressed
# index_db = true      # keep index.db beside the archive, for querying with SQL

[[job]]
name = "example.org"
server = "imap.example.org"
username = "jane@example.org"
# The password can come from a command instead, which needs --allow-exec:
# password_cmd = "gopass show -o mail/example.org"
password = ""
# folders = ["INBOX", "Sent"]   # leave out to back up every folder
"""


@dataclasses.dataclass
class InitResult:
    """What `archive init` made, and what it found already there.

    `made` lists the directories and the mark, for a caller that wants to know.
    No report prints them: which parts an archive consists of is the archive's
    business, and `git init` does not enumerate `.git` either.
    """

    created: bool = False
    made: list[str] = dataclasses.field(default_factory=list)
    config: pathlib.Path | None = None
    config_existed: bool = False


def init_archive(path: pathlib.Path, config_name: str) -> InitResult:
    """Make `path` an archive, or say why it cannot be one.

    Refused in one case, and it is the one that would do damage: a directory
    that holds something already but carries no mark. That is either an archive
    from before the mark existed -- writing the mark on it would claim a layout
    it is not in yet, and every command afterwards would look for its messages
    in the wrong place -- or it is not an archive at all and somebody is in the
    wrong directory. `archive migrate` is the answer to the first, `cd` to the
    second, and this cannot tell them apart, so it names both.

    A directory that is not there yet is made, parents included -- `git init
    some/where` does not ask for the directory to exist first either.

    Running it again on an archive is not an error: it reports what was already
    there. An existing configuration is never touched -- it holds credentials,
    and nothing here has any business replacing it.
    """
    result = InitResult()
    if path.exists() and not path.is_dir():
        raise JobError(f"{path}: not a directory")
    if marker.is_archive(path):
        log.info("%s: already an archive", path)
    elif path.exists() and any(path.iterdir()):
        raise JobError(
            f"{path}: there is already something here, and it is not a mailvault"
            f" archive. If it is an old mailvault archive, migrate it with"
            f" `mailvault archive migrate`. Otherwise you are in the wrong directory"
        )
    path.mkdir(parents=True, exist_ok=True)

    for name in (cas.MAIL_DIR, metalog.DEFAULT_LOG_DIR, heads.DEFAULT_HEADS_DIR):
        directory = path / name
        if not directory.is_dir():
            directory.mkdir(parents=True)
            result.made.append(f"{name}/")

    config = path / config_name
    result.config = config
    result.config_existed = config.is_file()
    if not result.config_existed:
        config.write_text(CONFIG_TEMPLATE, encoding="utf-8")

    if not marker.is_archive(path):
        marker.write(path)
        result.made.append(marker.FORMAT_NAME)
        result.created = True
    return result
