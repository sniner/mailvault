"""What an archive written by an older version left behind, and how to read it.

Everything in here exists for one purpose: so that `archive migrate` can lift an
archive it did not write. Nothing else in mailvault may import from this package
-- a running backup, check or verify never touches any of it, because a migrated
archive holds none of these artefacts any more.

That rule is what makes the package a package rather than a habit, and it has a
test that costs nothing to apply: **at 1.0, deleting this directory must leave a
working program.** Anything that breaks was in the wrong place.

Each module is named after the file it can read, because that is the whole of
what a legacy module is:

- `state_json` -- `state.json`, the resume state before `heads/`
- `store_db` -- `store.db`, the metadata database from before 0.8.0, when the
  archive still kept its truth in SQLite

Read-only by construction. None of these formats is ever written again, so
nothing here can write one: the readers carry no schema, nothing that creates
one, and no insert. What builds one is the test suite, which needs a specimen -- and keeping
the recipe there rather than here is what stops an old format from quietly
following a new one when the projection's schema moves.
"""
