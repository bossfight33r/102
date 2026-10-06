import structlog

from radar.log import MASK, configure_logging, redact_text


def test_redact_known_and_patterns(capsys):
    configure_logging("INFO", json=True, secrets=["super-secret-value"])
    log = structlog.get_logger()
    log.info(
        "event",
        api_key="raw-key",
        url="https://x/?key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456&q=1",
        note="token is super-secret-value",
        tg="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ1",
        nested={"telegram_bot_token": "x", "ok": "fine"},
        err=RuntimeError("sk-ant-api03-abcdef"),
    )
    out = capsys.readouterr().err
    for secret in ("raw-key", "super-secret-value", "AIzaSyABCDEF", "AAHdqTcvCH1v", "sk-ant-api03"):
        assert secret not in out
    assert "fine" in out and MASK in out


def test_redact_text_plain():
    assert "AIza" not in redact_text("key AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456")
