import pytest


@pytest.mark.smoke
def test_qapbot_error_str_includes_context_when_present():
    from qapbot.exceptions import QapBotError

    exc = QapBotError("boom", context={"user_id": "123"})
    assert exc.message == "boom"
    assert exc.context["user_id"] == "123"
    assert "boom" in str(exc)
    assert "user_id=123" in str(exc)


@pytest.mark.smoke
def test_specific_exception_is_qapbot_error_subclass():
    from qapbot.exceptions import ConfigurationError, QapBotError

    exc = ConfigurationError("bad config")
    assert isinstance(exc, QapBotError)
    assert exc.message == "bad config"
