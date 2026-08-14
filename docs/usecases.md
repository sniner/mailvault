# Use cases -- recipes for what people actually do

The [README](../README.md) shows the commands and
[Providers](providers.md) what a particular mailbox wants. This page is the
third question: *I have this situation -- how do I set it up?* Each recipe says
what it is for, what goes into `mailvault.toml`, and what to watch out for.


## Rolling old mail off a full mailbox

**The situation.** The mailbox is filling up. Mail older than about two years
should come off the server -- but nothing may be lost, so it has to be in the
archive first, and provably so.

**There is no `older_than` option, and there will not be one.** A backup run
carries on where the last one stopped, which is what makes a nightly run cost
only the new mail. A run that deliberately skips the *newest* mail cannot also
record that it is done with the folder: it would either claim mail it never
took -- and then never look at it again -- or it would have to read the whole
folder from the start every night, forever. Neither is worth an option.

What decides "older than two years" is not a backup tool anyway. It is your
mail system, which already has rules, and which knows what "old" means for your
mail better than a date arithmetic in a config file. So the answer is not a
filter. It is a folder.

**The recipe.**

1. Make a folder in the mailbox -- say `To Archive`.
2. Let old mail move into it: a server-side rule, a scheduled task, an Outlook
   rule, a Sieve script, or by hand. This is the step that decides what is old.
3. Give that folder a job of its own, with `delete_after_export`:

```toml
[[job]]
name = "example.org-sweep"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
folders = ["To Archive"]
delete_after_export = true
```

4. Run it -- on its own schedule, separately from the nightly backup:

```console
$ mailvault backup --job example.org-sweep --allow-exec
```

Everything sitting in the folder is archived and then removed from the server.
The folder is empty afterwards and stays empty until the next batch is moved in.
Nothing is deleted that did not make it into the archive, and nothing is deleted
before the record of where it was seen is safely written down.

> [!WARNING]
> **`delete_after_export` belongs to the job, not to the folder.** That is why
> this is a job of its own, with `folders` naming the one folder it may touch.
> Adding `delete_after_export` to a job that also backs up `INBOX` empties your
> inbox into the archive as well.

**A new job name needs saying so once.** mailvault refuses a job that has never
written into this archive before -- that is the check that catches the wrong
configuration against the wrong archive. The first run of the new job wants
`--allow-new-mailbox`; after that it is a familiar name and the flag is not
needed again. (Reusing the existing job's `name` avoids both the flag and a
second entry in `archive places`. Then `--job` selects the two jobs together,
which is the reason not to.)

**What "removed from the server" means depends on the provider.** On plain IMAP
the message is gone. On Gmail it moves to the trash and on Microsoft 365 into
Deleted Items, so the mailbox does not actually shrink until you say more --
[Deleting from the server](deep-dive.md#deleting-from-the-server-after-export)
has the option for each.

**Try it small.** Move three messages into the folder and run the job. Or leave
`delete_after_export` out for the first run: the mail is archived, nothing
happens on the server, and `mailvault archive places` shows it arrived.

**Why moving mail into a folder is enough to have it picked up.** A message
keeps its original date when it is moved, so a two-year-old mail lands in the
folder still looking two years old -- but it is new *to that folder*, and that
is what a backup run goes by. It is picked up on the next run like anything
else that arrives there.

**Your ordinary backup can stay as it is.** Run the complete mailbox backup
alongside; a message that both jobs see is stored once and recorded in both
places. And once it is archived, the archive is where you ask for it:

```console
$ mailvault db search --until 2024-01-01 --mailbox example.org
```
