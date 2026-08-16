"""Getting the original message out of an Exchange journal envelope.

A journal item is a cover message with the real one attached, and what a backup
must archive is the message, not the envelope. Kept apart from the rest of the
package because none of it is about reading a field: it is one vendor's wrapper
and one vendor's bounce, and the workarounds belong where they can be read as
such.
"""

from __future__ import annotations

import collections.abc
import email.message
import email.policy
import logging
import typing

from mailvault.mailutils.reading import decode_email

log = logging.getLogger(__name__)


def unwrap_exchange_journal_item(msg: typing.BinaryIO | bytes) -> bytes | None:
    """Returns None if not a journal item. Binary RFC822 message otherwise."""

    def as_bytes(m: email.message.Message) -> bytes | None:
        """The attached message written back out, or None if it cannot be.

        Both attempts are the same one twice, once with a policy that may hold
        utf-8 where the other cannot. What is parsed here always comes from
        bytes, and its non-ASCII arrives as surrogates that either policy writes
        out unchanged -- the encode error these guard against needs a payload
        that was never bytes, and there is no way to get one on this path.
        """
        try:
            return m.as_bytes(policy=email.policy.SMTP)
        except UnicodeEncodeError:
            log.debug("as_bytes: email.policy.SMTP failed")
        try:
            return m.as_bytes(policy=email.policy.SMTPUTF8)
        except UnicodeEncodeError:
            log.debug("as_bytes: email.policy.SMTPUTF8 failed")
        return None

    def rfc822_attachment(
        parts: collections.abc.Sequence[email.message.Message],
        idx: int,
    ) -> bytes | None:
        # `get_payload()` returns the parts of a multipart, the string of a
        # simple body, or None -- which of them depends on the message. This
        # one is a `message/rfc822` part, whose payload is always the single
        # message inside it.
        payload = typing.cast(list[email.message.Message], parts[idx].get_payload())
        submsgs = [as_bytes(m) for m in payload]
        if len(submsgs) == 1:
            return submsgs[0]
        return None

    cover = decode_email(msg)
    parts = [part for part in cover.walk() if part.get_content_type() == "message/rfc822"]

    # WORKAROUND: Microsoft Exchange sends journal messages using the original
    # sender address. When DKIM/SPF is configured for the sender's domain, the
    # receiving SMTP server rejects the message. Microsoft then wraps the
    # original journal entry in an "Undeliverable: <SUBJECT>" bounce, which
    # contains two RFC822 attachments instead of one.
    if len(parts) > 0:
        # When multiple RFC822 attachments are present, check whether this is
        # an "Undeliverable" bounce. In that case the first attachment is a
        # malformed delivery status (starts with "Content-Type:") and the
        # actual journal message is the second attachment.
        submsg = rfc822_attachment(parts, 0)
        if submsg and submsg.startswith(b"Content-Type:"):
            submsg = rfc822_attachment(parts, 1)
            if submsg:
                log.warning("Message was rescued from 'Undeliverable' stupidity")
        return submsg
    return None
