# Sealing an archive: proving that nothing was changed

An archive made by mailvault is often the only copy of the mail in it. Sometimes
you have to show someone else, an auditor, a tax office, a court, that it still
holds what it held at a certain time: that no message was removed or altered
since.

A seal does that. `mailvault archive seal` records the state of the archive and
prints an id for it. Whoever keeps that id can later check the archive against
it. Either everything the archive held then is still there and unchanged, or the
check names what is missing or different.

* [What a seal is for](#what-a-seal-is-for)
* [What a seal proves, and what it does not](#what-a-seal-proves-and-what-it-does-not)
* [Sealing the archive](#sealing-the-archive)
* [Keeping the id](#keeping-the-id)
* [Checking a seal](#checking-a-seal)
* [Listing the seals](#listing-the-seals)
* [Timestamping the id](#timestamping-the-id)
* [Checking without mailvault](#checking-without-mailvault)
* [What changes for the archive](#what-changes-for-the-archive)


## What a seal is for

Businesses often have to keep their correspondence for years, and keep it
unchanged. In Germany the GoBD, the tax administration's rules for keeping
business records in electronic form, cover email that serves as business
correspondence. Among other things, a record must not be changed in a way that
makes its original content impossible to determine, or that leaves it uncertain
whether a change was made at the start or later. Other countries have comparable
rules.

> **This page is not legal advice.** mailvault does not claim that an archive
> meets the GoBD or any other rule. Whether it does depends on the whole
> procedure: who runs the archive, where it is kept, who can write to it, how
> all of this is documented, and what else is in place. A seal adds one thing:
> a way to show whether the archive has changed since a given moment. Ask
> whoever is responsible for compliance in your organisation whether that
> serves your purpose.

A seal does not stop anyone from changing the archive. The archive is ordinary
files on ordinary storage, and whoever can write to it can change it, seals
included. What a seal provides is that a change can be shown afterwards, or
ruled out.


## What a seal proves, and what it does not

A seal that holds proves that **everything the archive held when the seal was
made is still there, unchanged.** That includes changes that leave the archive
looking perfectly in order, for example a message removed together with every
record of it. `mailvault archive check` cannot find such a change, because
nothing in the archive points at what is missing. A seal can.

It does **not** prove:

* **Anything about the time before sealing.** Whoever had the archive before the
  seal was made could have changed it. Sealing early and regularly keeps that
  gap small.
* **When the seal was made, on its own.** The seal carries a date, written by
  whoever made it. The date cannot be changed later without changing the id, but
  it can have been wrong from the start. What proves the time is where the id
  went: see [Keeping the id](#keeping-the-id).
* **Anything about mail added later.** A backup after the seal adds mail the seal
  does not cover. A check reports it as "recorded since" and does not count it
  as sealed. It cannot tell mail from a later backup from mail somebody put
  there.
* **That the archive is complete.** A seal records what the archive held, not
  what the mailbox held.

A seal shows *that* something changed and exactly *which* files, but it cannot
bring back what is gone. For that you need a backup of the archive. Because the
seal names every missing or changed file precisely, the right copy can be found
in a backup, and once it is restored the seal holds again. **Seal and backup
belong together.**


## Sealing the archive

```console
$ mailvault archive seal
2026-10-22 02:15:07,113 INFO -- START
2026-10-22 02:15:07,113 INFO -- Archive: /srv/archive/company
2026-10-22 02:15:07,114 INFO -- checking the checksums of 60 log files
2026-10-22 02:15:07,402 INFO -- sealed 60 log files covering 130,887 messages in 59 places
2026-10-22 02:15:07,402 INFO -- first seal of this archive: from now on `archive compact` refuses to run
2026-10-22 02:15:07,402 INFO -- keep this id outside the archive; `archive seal --check ID` checks it
1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
2026-10-22 02:15:07,402 INFO -- FINISHED
```

The long line is the seal id. It is the only line on stdout, so a script can
take it with `id=$(mailvault archive seal)`; everything else goes to stderr.

What a seal records is the archive's metadata log: the files that say which
message was seen in which mailbox and folder. Each of them names its messages by
their checksum, so recording the log fixes every message as well. Sealing does
not read the messages and takes seconds. Checking a seal reads them all.

**Run it again** when nothing was recorded since, and it prints the same id
instead of making a new seal. So it can run after every backup without piling
up seals.

**Before the first seal**, two things are worth doing:

* Run `mailvault archive check`. A seal records the archive as it is, damage
  included, and a message that was already damaged would later look as if it
  had been changed after sealing.
* Run `mailvault archive compact` if the metadata log has grown over the years.
  Once the archive holds a seal, `compact` refuses to run (see [What changes for
  the archive](#what-changes-for-the-archive)).

`--dry-run` says what would be sealed, reminds you of `compact`, and writes
nothing:

```console
$ mailvault archive seal --dry-run
would seal 1,204 log files covering 130,887 messages in 59 places; nothing was written
  this would be the first seal of this archive: from then on `archive compact` refuses to run, so compact first if you want the log consolidated
```

### When sealing is refused

A seal is not made over an archive that is already disturbed, because the seal
could never be checked, or would cover up a loss. Each refusal names the file
and the way out:

| What it found | Way out |
|---|---|
| A log file is damaged | Restore it from a backup of the archive and seal again. Without a backup: delete all seals, run `archive compact`, then seal again |
| A log file that an earlier seal records is gone | Restore it from a backup and seal again. Without a backup: delete the seals that record it, then seal again. Their ids fail their check from then on, which is true: the file is gone |
| A seal is damaged | Restore it from a backup, or delete it |
| A seal was written by a newer mailvault | Update mailvault |

```console
$ mailvault archive seal
2026-10-23 02:15:08,551 ERROR -- meta/a8/a850ed58….jsonl is gone, and the seal of 2026-10-22 02:15:07 UTC records it
2026-10-23 02:15:08,551 ERROR -- a new seal would hide the loss: restore it from a backup of the archive and seal again; without a backup, delete the seals that record it first (their ids then fail their check)
```

The command exits non-zero and prints no id.


## Keeping the id

The id is worth as much as the place it is kept. Inside the archive it proves
nothing: whoever can change the archive can change the id as well. Keep it where
the people who can write to the archive cannot change it:

* printed, dated and signed, filed with your other records
* in a mail to someone else: the accountant, the auditor, a colleague who does
  not run the archive
* with a timestamp service (see [Timestamping the id](#timestamping-the-id))

That place is also what proves **when** the seal was made. A seal whose own date
is later than the date on the printout, or than the mail it was sent in, is a
finding in itself.

### Sealing after every backup

The simplest routine is a seal after every backup, in the same scheduled job:

```console
$ crontab -l
15 2 * * *  mailvault --archive /srv/archive/company backup && mailvault --archive /srv/archive/company archive seal
```

With `&&` the archive is sealed only after a backup without errors; with `;` it
is sealed either way. If cron mails the output of its jobs to someone, that mail
carries every night's id out of the archive. A night without new mail produces
no new seal, only the same id again.


## Checking a seal

```console
$ mailvault archive seal --check 1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
recorded since: 14 log files, 1,203 messages
the seal of 2026-10-22 02:15:07 UTC holds: everything the archive held then (60 log files, 130,887 messages) is present and unchanged
```

The last line is the verdict, and it names the time of the seal, because that
is what it is about: the archive still holds what it held at that time. The
line before it is what the archive holds beyond the seal, from backups since;
the seal says nothing about that mail.

The check reads every message the seal covers, in full. On a large archive on a
network share that takes a while, about as long as the second half of `archive
check`. The exit code is zero when the seal holds and non-zero otherwise.

**The id is checked only in full.** The first few characters of an id prove
nothing: a forged seal can be made to start the same way. mailvault refuses a
shortened id.

**Any copy of the archive can be checked,** a restored backup included, and it
needs no configuration. The id itself tells which checksum it uses:

```console
$ mailvault --archive /mnt/restore/company archive seal --check 1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
```

### When a seal does not hold

```console
$ mailvault archive seal --check 1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
1 sealed log file gone
  meta/a8/a850ed58….jsonl
2 sealed messages gone
  6f3ac1…  mail.example.org::INBOX
  9d0e47…  mail.example.org::INBOX
the seal of 2026-10-22 02:15:07 UTC does not hold: 3 findings above
```

A finding is a log file or a message that is gone, or whose content no longer
matches its checksum, with the mailbox and folder a message was recorded in.
Two questions follow from it: what happened, and how to get the files back. The
second is answered by a backup of the archive. Restore the named files, check
again, and the seal holds again.

If the seal itself is gone, the check says so:

```console
$ mailvault archive seal --check 1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
the archive holds no seal 1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776, so it cannot be checked
```

That is a finding as well: either somebody deleted the seal, or the id belongs
to a different archive.


## Listing the seals

```console
$ mailvault archive seal --list
2026-10-20 02:14:51 UTC  a1964fb8d978a180f83720469ae975975b80a789b026fc35151cff6fe47520f9
2026-10-21 02:13:22 UTC  387406a22c0fd57c6354ac516660722dca5fca5e963e10aebc84ef219eec2722
2026-10-22 02:15:07 UTC  1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776
```

Oldest first, each with its full id. A seal that cannot be read is listed with
the reason instead of a date, never left out. Every seal ever made stays in the
archive, so every id given out can still be checked, however many backups ran
since.


## Timestamping the id

A printout with a date and a signature is often enough. Where it is not, a
timestamp service can confirm the time instead: it signs "this id existed at
this moment" with its own certificate. Such services follow a standard, RFC
3161, and they receive only the id, nothing from the archive. In the EU, a
qualified electronic timestamp under the eIDAS regulation carries a legal
presumption that its time is correct; ordinary services are free but carry no
such presumption.

mailvault does not do this itself. Which service to use, whether it has to be a
qualified one, and where the receipts are kept is your organisation's decision,
and it is easy to do from a script, for example with `openssl`:

```console
$ id=$(mailvault --archive /srv/archive/company archive seal)
$ openssl ts -query -digest "$id" -sha256 -cert -out seal.tsq
$ curl -s -H 'Content-Type: application/timestamp-query' \
      --data-binary @seal.tsq https://tsa.example.org/tsr > seal.tsr
$ openssl ts -verify -in seal.tsr -queryfile seal.tsq -CAfile tsa-ca.pem -untrusted tsa.pem
Verification: OK
```

Keep the receipt (`seal.tsr`) and the service's certificates outside the
archive, for the same reason as the id: the signature protects the receipt
against being changed, not against being deleted. For an archive whose ids are
96 characters long, write `-sha384` instead of `-sha256`.


## Checking without mailvault

An auditor's IT department may want to check a seal without relying on the
program that made it. Everything needed is plain files and standard checksums.
In an archive whose ids are 64 characters long that is SHA-256 (`sha256sum`), in
one whose ids are 96 characters long SHA-384 (`sha384sum`).

1. **The seal** lies in `meta/`, in a directory named after the first two
   characters of its id: `meta/1b/1bbb4b0c….seal`. Its checksum is the id.

   ```console
   $ sha256sum meta/1b/1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776.seal
   1bbb4b0cc3a691061154ec0a228cb409aa3266c5bb90a7182c9164b02e153776  meta/1b/…
   ```

2. **Every line after the first** names a log file, `{"log": "a850ed58…"}`, which
   lies at `meta/a8/a850ed58….jsonl`. Its checksum is its name.

3. **Every line after the first in a log file** names a message,
   `{"store_id": "6f3ac1…"}`, which lies at `mail/6f/3a/6f3ac1….eml`. Its
   checksum is its name. A message stored compressed ends in `.eml.zst` and is
   checked uncompressed: `zstd -dc mail/6f/3a/6f3ac1….eml.zst | sha256sum`.

If every file is there and every checksum matches its name, the seal holds.


## What changes for the archive

* **`archive compact` refuses to run** once the archive holds a seal. Compacting
  replaces the log files a seal records, and the seal could no longer be
  checked. Compact before the first seal. To compact later anyway, delete the
  seals first (they lie in `meta/` and end in `.seal`); every id given out for
  them then fails its check.

  ```console
  $ mailvault archive compact
  2026-10-23 09:12:40,018 ERROR -- this archive is sealed (14 seals, the newest of 2026-10-22 02:15:07 UTC): compacting the metadata log would change what the seals record, and their ids could no longer be checked. To go ahead anyway, delete the .seal files in meta/ first; every seal id given out so far then fails its check
  ```

* **Update every installation of mailvault** that works on the archive before
  the first seal. Older versions leave seals alone, but they do not know that
  `compact` must not run, and would compact a sealed archive. Nothing is lost if
  that happens: restore the log files it removed from a backup of the archive,
  and every seal holds again.
* **`archive check` checks the seals too,** and reports a damaged one.
* **Everything else only adds to the archive** and never breaks a seal: backups,
  `archive import`, `archive adopt`, `verify --repair`.
* **mailvault never removes a seal.** Deleting one by hand is possible, and every
  id given out for it fails its check from then on, which is what should happen.
