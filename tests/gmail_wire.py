"""What Gmail actually sends back, recorded off the wire on 2026-08-27.

Not a mock of a response: the token order and the spelling of X-GM-LABELS below
are the ones `imap.gmail.com` sent, captured during a read-only FETCH of
`BODY.PEEK[] X-GM-LABELS` and kept.

Substituted, because none of it belongs in a public repository and nothing under
test reads any of it: the message body, the sequence number, and the UID -- a UID
out of a real mailbox is an internal identifier of somebody's account and says
roughly how much mail has passed through it. What is recorded is the part that
carries the meaning: that the two items come back in one response, in that order,
with the label as an IMAP-quoted string and the empty case as `()`.

They exist because a mock of this response is worth exactly what its author knew,
and its author has been wrong about it: the tests that stood here before mocked
`get_gmail_labels` with a list of `bytes`, which the real call never returns. The
label below is an IMAP-quoted string, the empty case is an empty parenthesised
list, and neither of those is what anyone would have guessed.

What they cannot tell anybody is whether Gmail still answers this way. Only the
account can, and it does not run in CI.
"""

from __future__ import annotations

# Stands in for the body of a real message. `_places_from` never looks at it.
BODY = b"From: sender@example.com\r\nSubject: Recorded\r\n\r\nbody\r\n"

# A message in All Mail that is also in the inbox, so X-GM-LABELS names the one
# place the selected folder is not.
WITH_A_LABEL: list = [
    (b'1 (X-GM-LABELS ("\\\\Inbox") UID 101 BODY[] {53}', BODY),
    b")",
]

# A message whose only place is the folder it was read from. Gmail leaves out the
# label belonging to the selected folder, so what comes back is empty -- and the
# folder has to be added back, which is the whole of `places_read_from`.
WITH_NO_LABEL: list = [
    (b"2 (X-GM-LABELS () UID 102 BODY[] {53}", BODY),
    b")",
]
