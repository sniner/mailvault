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
3. Give that folder a job of its own, with `delete_after_export` -- under the
   *same* `name` as the job that already backs up this mailbox:

```toml
[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
folders = ["INBOX", "Archive", "Sent"]

[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
folders = ["To Archive"]
delete_after_export = true
```

4. Run the backup as usual:

```console
$ mailvault backup --allow-exec
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

**Two jobs, one name.** Nothing stops two `[[job]]` entries from carrying the
same `name`, and here that is the point: the name is the mailbox name in the
archive, so both jobs write into one mailbox. One entry in `archive places`, one
name to search under, and no `--allow-new-mailbox` -- the name has written here
before.

> [!IMPORTANT]
> **Then the two jobs must not read the same folder.** A job's name is also half
> of what a resume point belongs to: the archive remembers how far it got per
> *place*, and a place is a job name plus a folder. Two jobs of the same name
> share the resume point of every folder they both read -- so their `folders`
> lists have to be disjoint, with the sweep folder in exactly one of them.
>
> What goes wrong otherwise is quiet. The ordinary job reads `To Archive` first,
> archives what is in it and moves the shared resume point past it. The sweep job
> follows, finds nothing new, and a message it never fetched is not a message it
> may delete -- so the folder is never emptied. Nothing is lost, the mail is in
> the archive, but the deleting silently stops happening.

**Unless the ordinary job cannot name its folders.** Leaving `folders` out means
*all* folders, `To Archive` among them -- that is the recommended setting for
[iCloud](providers.md#icloud-apple-mail), which has no folder that holds
everything. Two ways out. Exclude the sweep folder there:

```toml
ignore_folder_names = ["To Archive"]
```

Or give the sweep job a name of its own, say `example.org-sweep`. A different
name is a different mailbox, with resume points of its own, so an overlap costs
nothing but the ordinary job reading a folder that is about to be emptied. It
does cost `--allow-new-mailbox` on the first run -- mailvault refuses a job that
has never written into this archive, which is the check that catches the wrong
configuration against the wrong archive -- and a second entry in `archive
places`, and mail under two names to search under.

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

**Your ordinary backup carries on unchanged**, apart from the one folder it now
leaves to the sweep. A message that both jobs do see is stored once all the same
-- the archive keeps one copy and writes down each place it was seen. And once it
is archived, the archive is where you ask for it:

```console
$ mailvault db search --until 2024-01-01 --mailbox example.org
```
