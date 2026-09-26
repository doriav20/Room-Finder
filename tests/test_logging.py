import logging

from roomfinder.main import RedactSecrets

TOKEN = "123456789:AAHsecretsecretsecretsecretsecret"


def make_record(message: str, *args) -> logging.LogRecord:
    return logging.LogRecord("test", logging.DEBUG, __file__, 1, message, args, None)


def test_secrets_are_redacted_from_messages_and_arguments():
    redact = RedactSecrets([TOKEN, ""])
    record = make_record("Set Bot API URL: %s", f"https://api.telegram.org/bot{TOKEN}")

    assert redact.filter(record) is True
    assert record.getMessage() == "Set Bot API URL: https://api.telegram.org/bot<redacted>"


def test_other_messages_are_untouched():
    record = make_record("found %d listings", 3)

    RedactSecrets([TOKEN]).filter(record)

    assert record.getMessage() == "found 3 listings"
    assert record.args == (3,)
