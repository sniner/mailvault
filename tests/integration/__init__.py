"""Tests that run against a real IMAP server instead of a mock of one.

Everything under `tests/` other than this talks to a `MagicMock` where the
server would be, which tests mailvault against mailvault's *belief* about
imapclient and the protocol. That belief has been wrong: a mocked
`get_gmail_labels` returned bytes the real one never returns, and the tests
built on it would have passed while the archive started recording one label
under two names.

These fill the gap the mocks cannot: a Dovecot in a container, spoken to over
the wire, with the folder names, UIDs and UIDVALIDITY a real server produces.
"""
