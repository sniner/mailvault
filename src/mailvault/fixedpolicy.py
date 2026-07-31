"""
Workaround for "UnicodeEncodeError: 'ascii' codec can't encode character '\\x..' in position ..: ordinal not in range(128)"
in msg_part.as_bytes() of Python 3.9.

Source: https://bugs.python.org/issue41307
"""

from __future__ import annotations

import email.policy
from copy import copy
from email._policybase import Compat32  # type: ignore
from email.generator import BytesGenerator, _has_surrogates  # type: ignore
from email.message import Message
from io import BytesIO


class FixedBytesGenerator(BytesGenerator):
    def _handle_text(self, msg):  # type: ignore
        payload = msg._payload
        if payload is None:
            return
        charset = msg.get_param("charset")
        if (
            charset is not None
            and self.policy is not None
            and not self.policy.cte_type == "7bit"
            and not _has_surrogates(payload)
        ):  # type: ignore
            msg = copy(msg)
            msg._payload = payload.encode(charset).decode("ascii", "surrogateescape")
        super()._handle_text(msg)  # type: ignore

    _writeBody = _handle_text  # type: ignore


class FixedMessage(Message):
    def as_bytes(self, unixfrom=False, policy=None):
        policy = self.policy if policy is None else policy
        fp = BytesIO()
        g = FixedBytesGenerator(fp, mangle_from_=False, policy=policy)
        g.flatten(self, unixfrom=unixfrom)
        return fp.getvalue()


compat32 = Compat32(message_factory=FixedMessage, linesep="\r\n")
SMTP = email.policy.EmailPolicy(message_factory=FixedMessage, linesep="\r\n")
SMTPUTF8 = email.policy.EmailPolicy(message_factory=FixedMessage, linesep="\r\n", utf8=True)
