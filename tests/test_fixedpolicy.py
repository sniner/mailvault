from mailvault import fixedpolicy


def test_fixed_message():
    msg = fixedpolicy.FixedMessage()
    msg["Subject"] = "Test"
    msg.set_payload("Body")
    b = msg.as_bytes(policy=fixedpolicy.SMTPUTF8)
    assert b"Subject: Test" in b
    assert b"Body" in b
