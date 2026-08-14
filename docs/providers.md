# Providers -- what each mailbox needs

Setting up a mailbox is mostly the same everywhere: a server, a login, a list of
folders. What differs is a handful of small things that are hard to guess --
which password is accepted, what the folders are called, which one to back up.

One section per provider, each with a `[[job]]` to copy. This page says what to
put in the file; what every option means and why things work the way they do is
in the [deep dive](deep-dive.md), which each section links to where there is
more to know.

* [What is the same everywhere](#what-is-the-same-everywhere)
* [Plain IMAP](#plain-imap)
* [Gmail and Google Workspace](#gmail-and-google-workspace)
* [Microsoft 365](#microsoft-365)
* [Proton Mail](#proton-mail)
* [iCloud (Apple Mail)](#icloud-apple-mail)

| Provider | The one thing to know |
|----------|-----------------------|
| Plain IMAP | Nothing -- this is the ordinary case |
| Gmail | Labels are folders, and `All Mail` already holds everything |
| Microsoft 365 | No password: an app registration in Azure, which can see the whole tenant |
| Proton Mail | Through the local Bridge, and only once the Bridge has finished syncing |
| iCloud | The server is `imap.mail.me.com`, which is not what anyone guesses |


## What is the same everywhere

**Ask the mailbox what its folders are called** before writing them down. The
names belong to the server, not to your mail program -- most of the providers
here either translate them or call them something else entirely:

```console
$ mailvault folders
gmail.com::INBOX
gmail.com::[Gmail]/All Mail
```

Leave `folders` out and everything the mailbox offers is backed up. That is the
right default nearly everywhere -- Gmail is the exception.

**Keep the password out of the file.** Any field can be given as `_cmd` instead,
which runs a command and takes what it prints. It only runs when you pass
`--allow-exec`:

```toml
password_cmd = "pass show email/example.org"
```

**Pick `name` once and keep it.** It is what this mailbox is called in every
report and in the archive's own records. Renaming it later leaves the mail
already archived under the old name.


## Plain IMAP

Most hosted mailboxes, and every mail server of your own. Nothing here is
special, which is why this is the shortest section:

```toml
[[job]]
name = "example.org"
server = "imap.example.org"
username = "john.doe@example.org"
password_cmd = "pass show email/example.org"
folders = ["INBOX", "Archive", "Sent"]
```

`port` is 993 and `tls` is on unless you say otherwise. To back up everything
*except* the folders nobody wants archived, leave `folders` out and name what to
skip instead:

```toml
ignore_folder_flags = ["Junk", "Drafts", "Trash"]
```

That works without knowing what the folders are called on this particular
server -- it goes by what the server says a folder is *for*, so it finds
`Papierkorb` as readily as `Trash`.

If the mailbox has two-factor authentication, your normal password will most
likely be refused and you need an app password. That is true of Gmail and iCloud
below, and of many others.


## Gmail and Google Workspace

```toml
[[job]]
name = "gmail.com"
server = "imap.gmail.com"
username = "john.doe@gmail.com"
password_cmd = "pass show email/gmail"
folders = ["[Gmail]/All Mail"]
```

**An app password, not your Google password.** You create one in the Google
account settings, and only after 2-step verification is switched on. The account
password is refused as if it were wrong.

**Back up `All Mail` and nothing else.** In Gmail a message is not *in* a folder,
it *carries labels* -- and every label shows up over IMAP as a folder. `All Mail`
is the one that holds the whole account, including what is in no other folder.
The rest overlap with it, so backing up everything means downloading each message
several times to store it once. It costs time and bandwidth and adds nothing to
the archive.

**The names are translated, including Google's own.** It is `[Gmail]/All Mail`
on one account and `[Google Mail]/Alle Nachrichten` on another, so let
`mailvault folders` tell you which it is here. A label you cannot see there is
hidden from IMAP in Gmail's label settings, and that is where to switch it on.

**Your labels are kept, even though you back up one folder.** mailvault notes
which labels each message carries, so you can look for them afterwards:

```console
$ mailvault db search --folder Invoices
```

One message is then in several places at once, which is worth knowing when
something is counted: `archive places` and `archive check` count places, so their
totals come out higher than the number of messages. Both say so where it happens.

**Deleting after export only moves to the trash.** With `delete_after_export`
the message lands in `[Gmail]/Trash` and the account does not actually get
smaller. There is a way to finish the job, and a warning to read first:
[Deleting from the server](deep-dive.md#deleting-from-the-server-after-export).


## Microsoft 365

Microsoft 365 is not backed up over IMAP but over Microsoft's own interface, MS
Graph -- which is also why this job has no `server`, no `port` and no password:

```toml
[[job]]
name = "example.com"
backend = "msgraph"
tenant_id = "00000000-0000-0000-0000-000000000000"
client_id = "11111111-1111-1111-1111-111111111111"
client_secret_cmd = "pass show m365/client-secret"
username = "john.doe@example.com"
folders = ["Inbox", "Sent Items", "Archive"]
```

**What to set up.** In Azure, register an application, give it the permission
`Mail.Read`, have an administrator approve it, and create a client secret. What
that leaves you with is `tenant_id`, `client_id` and the secret. It is worth the
detour: this kind of access is made for unattended backups -- nobody has to sign
in, and nothing expires over the weekend.

`Mail.Read` is enough to read mail. A job that also deletes after export, or
that files journal leftovers away, needs `Mail.ReadWrite` -- and says so plainly
if it is missing, rather than failing in some other way.

> [!WARNING]
> This kind of permission covers **every mailbox in the tenant**, not just the
> one in `username`. Have your administrator restrict it to that mailbox with an
> application access policy. And keep the secret out of the file with
> `client_secret_cmd`: it is worth considerably more than a mailbox password.

**`username` says which mailbox to read.** It is not a login -- the login is the
tenant, the client and the secret above it.

**Folder names are the ones you see in Outlook**, in the language of the mailbox:
`Posteingang` and `Gesendete Elemente` on a German tenant. `mailvault folders`
prints them. `ignore_folder_flags` does nothing here, because these folders carry
no such marks; `ignore_folder_names` works as usual.

**Repeated runs stay cheap**, and mail deleted in the mailbox in the meantime
does not throw the next run back to the beginning. Microsoft 365 tells mailvault
what has changed, and that includes what has gone:

```
example.com::Inbox: 1 message gone from the folder
```

A folder that has never held a message is read again from the start next time,
and says so:

```
example.com::Outbox: no messages offered, resume point not started
```

Nothing needs to be done about that. An empty folder and a mailbox that is not
answering properly yet look exactly alike from outside, so mailvault assumes the
second -- on an empty folder that costs one question and no mail.

The remaining options of this backend, and what it does differently from IMAP,
are in [MS Graph backend](deep-dive.md#ms-graph-backend).


## Proton Mail

Proton has no mail server you can connect to. Instead, **Proton Bridge** runs on
your own machine and offers the account there:

```toml
[[job]]
name = "proton.me"
server = "127.0.0.1"
port = 1143
tls = false
username = "john.doe@proton.me"
password_cmd = "pass show email/proton-bridge"
folders = ["All Mail"]
```

**The Bridge's password, not your Proton password.** For each account it shows
you an address, a port and a password of its own -- that is what goes into
`server`, `port` and `password`. The port is usually 1143.

**`tls = false` is correct here.** The Bridge offers no encrypted connection on
that port, and it does not need one: nothing leaves your machine. mailvault
warns once per run that the connection is unencrypted -- expected, in this one
case.

> [!IMPORTANT]
> **Wait until the Bridge has finished syncing.** It answers long before it has
> fetched the account, and until then it reports its folders as empty rather than
> as not ready yet. Nothing is lost if you start too early -- mailvault does not
> take an empty answer as proof that a folder is empty, so it simply reads that
> folder again next time.

**`All Mail` holds everything**, as on Gmail, and is the one to back up. Your
folders and labels sit beside it under `Folders/` and `Labels/`. Unlike Gmail,
Proton does not tell mailvault which labels a message carries, so a label only
becomes searchable in the archive if you back up that folder as well:

```toml
folders = ["All Mail", "Labels/Invoices"]
```

Why an early start costs nothing: [Proton Mail via
Bridge](deep-dive.md#proton-mail-via-bridge).


## iCloud (Apple Mail)

```toml
[[job]]
name = "icloud.com"
server = "imap.mail.me.com"
username = "john.doe@icloud.com"
password_cmd = "pass show email/icloud"
```

**The server is `imap.mail.me.com`.** Not `imap.icloud.com`, which is what
everybody tries first and which does not work. Port and encryption are the usual
ones, so there is nothing else to set.

**Your iCloud mail address, not your Apple ID.** If your Apple ID is some other
address, iCloud will not accept it here. What works is the `@icloud.com`,
`@me.com` or `@mac.com` address of the mailbox itself.

**An app-specific password**, created in your Apple account settings. The Apple
ID password is refused -- and worth checking twice, because iCloud answers a
failed login with a complaint about syntax that says nothing about the real
cause.

**Leave `folders` out.** iCloud has no folder that holds everything, so backing
up all of them is the sensible default here. If you do want to name them, note
that they are not called what Mail shows you -- `Sent Messages` rather than
*Sent*. Again, `mailvault folders` says.


## Anything else

Two things belong elsewhere, because they are not a question of the provider:

* **An Exchange journal mailbox** is archived differently -- every message it
  holds is an envelope with the actual mail inside, and that is what should end
  up in the archive. It works on both backends: [Exchange journal
  mailboxes](deep-dive.md#exchange-journal-mailboxes)
* **Every option there is**, for either backend, with its default and what it
  does: [configuration reference](deep-dive.md#configuration-reference)
