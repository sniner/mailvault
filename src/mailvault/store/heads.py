"""Where the next incremental run picks up, one small file per place.

A *place* is a mailbox and a folder within it, and `heads/` holds one file for
each with whatever is known about it. Two different things end up here, and they
do not cover the same places:

- a **resume point**, for a place that a job polls as a folder. It comes from
  the configuration, and it says where the next pass carries on
- the **chain head of the metadata log**, for a place a message was *seen* in

Those coincide for most backends and come apart for Gmail, which reports the
canonical labels of a message (`\\Sent`) where the configured folder name is a
localised view of the same thing (`[Google Mail]/Gesendet`). So a place may have
a resume point, or a chain head, or both.

This replaces the single `state.json` that used to hold the resume points, for
two reasons that are about reading rather than writing.

A backup calls `save()` after every folder. With one structure for all of them,
a run over forty folders wrote the whole thing forty times, thirty-nine of them
data that had not changed. That is the visible annoyance, and the smaller half.

The stronger argument is what a damaged file costs. `state.json` was decoded as a
whole, so one bad byte anywhere in it discarded every folder of every job -- a
full read of the entire archive's worth of mailboxes. One file per place makes
the same bad byte cost one folder. For the same reason the single-writer caveat
gets narrower: two runs over *different* mailboxes no longer write the same file.

Durability is deliberately **not** an argument here. The atomic rename covered
that already, and a lost write is benign -- the folder gets read in full once.
It is said out loud so nobody later adds it as a reason and wonders why it does
not hold up.

`heads/` is **not** a content-addressed store, and that is the distinction most
easily missed: the name is a function of the *place*, and the content changes.
A head therefore carries no integrity check of its own, unlike everything in
`meta/` and `mail/`. That is also why the chain head of the metadata log lives
in here as a single hash rather than a list -- see `metalog`.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import hashlib
import json
import logging
import pathlib
import re
from datetime import datetime

from mailvault.store import atomic

log = logging.getLogger(__name__)

# Default directory of the resume points inside an archive.
DEFAULT_HEADS_DIR = "heads"

# Payload format version, so a change can be recognised rather than guessed at.
HEAD_VERSION = 1
SUPPORTED_HEAD_VERSIONS = (HEAD_VERSION,)

# How much of the name may be the readable part. Both this and 100 stay far
# under the hard limit of 255 bytes per path component, so the deciding factor
# is elsewhere: mailvault ships Windows executables, and there MAX_PATH counts
# 260 for the *whole* path -- an archive that lies deep plus a long file name
# gets closer to that than it needs to. Encrypted filesystems (eCryptfs) cap
# names near 143. What truncation costs is legibility, never identity.
SLUG_LIMIT = 80

# What a slug part becomes when the name it came from holds no alphanumerics at
# all -- a folder called `→` or `...`. A whole part is never exactly this
# otherwise: the normalisation joins runs of alphanumerics with `_`, so no run
# yields the empty string, one run yields the run itself, and two yield `a_b`.
# The placeholder is therefore unambiguous rather than merely unlikely.
EMPTY_PART = "_"

# Runs of these make up the readable part; everything else separates them.
_ALNUM = re.compile(r"[A-Za-z0-9]+")

# What a head file is called: a readable part, a dot, and eight hex characters.
# Used to enumerate them, which is why it has to exclude the transient file of
# an interrupted write -- that one ends in `._tmp_` and does not match.
_HEAD_NAME = re.compile(r".+\.[0-9a-f]{8}\Z")


def _slug(text: str) -> str:
    """The readable part of a name, derived from a job or folder name.

    Leading and trailing junk disappears instead of becoming underscores:
    `[Google Mail]/Alle Nachrichten` gives `Google_Mail_Alle_Nachrichten`, not
    `_Google_Mail__Alle_Nachrichten`.
    """
    parts = _ALNUM.findall(text)
    return "_".join(parts) if parts else EMPTY_PART


def _identity(job: str, folder: str | None) -> str:
    """Eight hex characters that tell two places apart, whatever they are called.

    The slug is lossy and its collisions are not contrived: `INBOX/Sent` and
    `INBOX.Sent` are different folders on servers that differ only in their
    hierarchy separator, `Ruhl-Projekte` and `Ruhl Projekte` normalise the same,
    and so does anything that agrees for the first however-many characters. A
    collision would mean two places sharing one file, and the second one's
    resume point overwriting the first -- a UID watermark from folder A applied
    to folder B skips mail silently, which is the one fault this whole
    arrangement exists to rule out.

    Three things about how it is computed matter:

    - it goes over the **original** strings, not the slug, or it would fail to
      separate precisely the cases it is for -- colliding names share a slug
    - the separator is NUL, which cannot occur in either part, so `("a", "b/c")`
      and `("a/b", "c")` do not collapse into one value. A place whose folder is
      not known at all takes a second NUL, which no real name can produce either
      -- otherwise it would collide with a folder whose name is empty
    - case is preserved. IMAP folder names are case-sensitive apart from INBOX,
      and folding would manufacture collisions that do not exist

    blake2b rather than anything else for two practical reasons and no security
    one -- it separates names, it protects nothing. `digest_size=4` yields eight
    hex characters directly, with no truncation whose admissibility somebody has
    to think about later; and it is visibly not the sha384 of a store id, so no
    error message can be misread as naming one. Four bytes are generous for a
    value that only has to separate places sharing a slug, realistically two.
    """
    tail = "\0" if folder is None else folder
    raw = f"{job}\0{tail}".encode()
    return hashlib.blake2b(raw, digest_size=4).hexdigest()


def head_name(job: str, folder: str | None) -> str:
    """The file name a place is recorded under: readable part, dot, identity.

    The readable part is a reading aid and may be cut; the eight hex characters
    are the identity and may not.

    A place whose folder is not known -- the mailbox is, the folder is not, which
    the metadata log represents rather than guesses -- gets **no folder part at
    all**: `gmail_com.3f9a1c2b`. The shape says so by itself, and no folder name
    can produce it, because a folder always yields a part, if only the `_`
    placeholder.
    """
    slug = _slug(job) if folder is None else f"{_slug(job)}-{_slug(folder)}"
    if len(slug) > SLUG_LIMIT:
        # Cutting can leave a trailing separator or a run of underscores, which
        # says nothing and reads like a mistake.
        slug = slug[:SLUG_LIMIT].rstrip("_-") or EMPTY_PART
    return f"{slug}.{_identity(job, folder)}"


def head_path(root: pathlib.Path, job: str, folder: str | None) -> pathlib.Path:
    """Where the head of one place lives."""
    return root / head_name(job, folder)


@dataclasses.dataclass
class Head:
    """What is known about one place: who it is, when it was read, where to go on.

    `job` and `folder` are held in plain text, and they are not decoration: a
    reader compares them against what it was looking for, and a mismatch counts
    as no resume point at all. That is what makes a slug collision expensive but
    never wrong -- the worst case degenerates to two folders quietly
    invalidating each other, instead of one resuming from the other's position.
    """

    job: str
    folder: str | None
    last_run: str | None = None
    resume: dict | None = None
    # The chain head of the metadata log for this place. A single hash, because
    # this file carries no integrity check of its own: everything behind that
    # hash lives in files whose names *are* their hashes, so one value here
    # vouches for all of them. A list would be unattested.
    log: str | None = None

    def last_run_at(self) -> datetime | None:
        """When a run last read this place, or None if the value is unusable.

        A timestamp without a zone is read as local time, because that is what
        it was: such values come from an archive whose `state.json` was written
        by a version that used `datetime.now()` rather than `datetime.now(UTC)`.
        """
        if self.last_run is None:
            return None
        try:
            parsed = datetime.fromisoformat(self.last_run)
        except ValueError:
            log.warning(
                "%s::%s: unparsable timestamp %r, treating as unknown",
                self.job,
                self.folder,
                self.last_run,
            )
            return None
        return parsed if parsed.tzinfo is not None else parsed.astimezone()

    def to_payload(self) -> dict:
        return {
            "version": HEAD_VERSION,
            "job": self.job,
            "folder": self.folder,
            "last_run": self.last_run,
            "resume": self.resume,
            "log": self.log,
        }


def _is_usable_resume(value: object) -> bool:
    """A resume point has to be an object that names its kind, and no more.

    Everything past `kind` belongs to the backend that wrote it, so checking it
    here would only mean changing this module every time a backend learns
    something new.
    """
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    return isinstance(kind, str) and bool(kind)


def _decode(path: pathlib.Path, payload: object) -> Head | None:
    """Turn a decoded payload into a Head, or None when it cannot be trusted.

    Validated field by field. The file lies in an archive other tools may touch,
    and everything in it can be recovered by reading the folder in full -- so
    anything unusable has to degrade into "read it all" rather than into an
    AttributeError in the middle of a run.
    """
    if not isinstance(payload, dict):
        log.warning("%s: expected a JSON object, ignoring it", path)
        return None
    version = payload.get("version")
    if version not in SUPPORTED_HEAD_VERSIONS:
        log.warning("%s: unknown head version %r, ignoring it", path, version)
        return None
    job = payload.get("job")
    folder = payload.get("folder")
    if not isinstance(job, str) or not (folder is None or isinstance(folder, str)):
        log.warning("%s: does not say which place it belongs to, ignoring it", path)
        return None

    last_run = payload.get("last_run")
    if last_run is not None and not isinstance(last_run, str):
        log.warning("%s: non-string last_run, dropped", path)
        last_run = None

    resume = payload.get("resume")
    if resume is not None and not _is_usable_resume(resume):
        # Not worth failing over: an unusable resume point means the folder is
        # read in full, which is the safe outcome anyway.
        log.warning("%s: unusable resume point, the folder is read in full", path)
        resume = None

    chain = payload.get("log")
    if chain is not None and not isinstance(chain, str):
        log.warning("%s: non-string log head, dropped", path)
        chain = None

    return Head(job=job, folder=folder, last_run=last_run, resume=resume, log=chain)


def read_file(path: pathlib.Path) -> Head | None:
    """Read one head file, returning None when it is missing or unusable.

    A missing file is the normal case for a place nobody has backed up yet.
    """
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("%s: unreadable, treating the place as unknown: %s", path, exc)
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("%s: not valid JSON, treating the place as unknown: %s", path, exc)
        return None
    return _decode(path, payload)


def read(root: pathlib.Path, job: str, folder: str | None) -> Head | None:
    """The head of one place, or None to read that folder in full.

    A file whose `job`/`folder` do not match what was asked for is a slug
    collision, and it counts as no head at all: whatever is in it belongs to a
    different folder, and resuming from another folder's position is how mail
    gets skipped.
    """
    path = head_path(root, job, folder)
    head = read_file(path)
    if head is None:
        return None
    if (head.job, head.folder) != (job, folder):
        log.warning(
            "%s: holds %s::%s, not %s::%s -- two places share a name, so this folder"
            " is read in full",
            path,
            head.job,
            head.folder,
            job,
            folder,
        )
        return None
    return head


def write(root: pathlib.Path, head: Head) -> None:
    """Replace the head of a place, atomically."""
    body = json.dumps(head.to_payload(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic.write_text(head_path(root, head.job, head.folder), body)
    log.debug("%s::%s: resume point written", head.job, head.folder)


def head_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every head file below `root`, in a stable order.

    The transient file of an interrupted write does not match the name pattern
    and is skipped, the same way the metadata log skips its own.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and _HEAD_NAME.match(p.name))


def read_all(root: pathlib.Path) -> collections.abc.Iterator[Head]:
    """Yield every readable head. Unusable ones are warned about and skipped."""
    for path in head_files(root):
        head = read_file(path)
        if head is not None:
            yield head


def mailboxes(root: pathlib.Path) -> set[str]:
    """The mailboxes an archive has heads for -- who has written into it.

    The names cannot be read off the file names: a slug is lossy and the
    identity is a hash, so neither can be turned back into a job name. The
    files themselves are asked instead, which is cheap -- there are as many as
    there are folders, not as there are messages.
    """
    return {head.job for head in read_all(root) if head.job}
