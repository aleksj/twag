import logging

from twag_clickhouse.tool_server import (
    UVICORN_SCANNER_NOISE,
    _configure_uvicorn_logging,
    favicon,
    root,
)


def test_tool_server_root_and_favicon_are_probe_friendly() -> None:
    root_response = root()
    favicon_response = favicon()

    assert root_response["endpoints"]["query"] == "/query"
    assert favicon_response.status_code == 204


def test_uvicorn_scanner_noise_filter_suppresses_invalid_http_warning(
    caplog,
) -> None:
    _configure_uvicorn_logging()
    logger = logging.getLogger("uvicorn.error")

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        logger.warning(UVICORN_SCANNER_NOISE)
        logger.warning("real warning")

    messages = [record.getMessage() for record in caplog.records]
    assert UVICORN_SCANNER_NOISE not in messages
    assert "real warning" in messages
