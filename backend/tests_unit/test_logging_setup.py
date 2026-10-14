"""
Phase D session 6 live test: the app's own warnings (the billing guard, the duplicate-subscription
runbook line, BYO check failures) never showed in the uvicorn terminal. app/main.py now gives the
`app` logger its own stderr handler. These tests prove an app.* WARNING reaches that handler,
formatted, exactly once.

pytest attaches its own capture handlers (LogCaptureHandler, itself a StreamHandler subclass)
to the root logger and, via conftest, to `app`, so the app's handler is picked out by exact type.
"""

import io
import logging

import app.main as app_main


def _app_stream_handlers() -> list[logging.StreamHandler]:
    return [h for h in logging.getLogger("app").handlers if type(h) is logging.StreamHandler]


def test_the_app_logger_has_one_formatted_warning_handler_and_does_not_propagate():
    app_logger = logging.getLogger(app_main.APP_LOGGER_NAME)
    [handler] = _app_stream_handlers()
    assert handler.formatter._fmt == app_main.LOG_FORMAT
    assert app_logger.level == logging.WARNING
    assert app_logger.propagate is False  # a root config can never duplicate its lines


def test_a_warning_from_app_billing_reaches_the_handler_and_info_does_not():
    [handler] = _app_stream_handlers()
    captured = io.StringIO()
    original = handler.setStream(captured)
    try:
        logging.getLogger("app.billing").warning("billing unavailable: %s", "probe check")
        logging.getLogger("app.billing").info("an info line that must not appear")
        logging.getLogger("app.gating").warning("gating probe")
    finally:
        handler.setStream(original)
    output = captured.getvalue()
    assert "WARNING app.billing billing unavailable: probe check" in output
    assert "WARNING app.gating gating probe" in output
    assert "must not appear" not in output
    assert output.count("billing unavailable") == 1


def test_configuring_again_never_stacks_a_second_handler():
    app_main.configure_stderr_logger(app_main.APP_LOGGER_NAME, logging.WARNING)
    app_main.configure_stderr_logger(app_main.APP_LOGGER_NAME, logging.WARNING)
    assert len(_app_stream_handlers()) == 1
