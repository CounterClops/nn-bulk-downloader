"""Tests for download_image retry logic in the multporn module."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from modules.multporn import download_image, MAX_DOWNLOAD_RETRIES, DOWNLOAD_RETRY_DELAY


def _make_response(status_code: int, content: bytes = b"img") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = content
    return resp


def _make_http_error(status_code: int) -> requests.HTTPError:
    resp = _make_response(status_code)
    err = requests.HTTPError(response=resp)
    return err


class TestDownloadImageRetry:
    def test_success_on_first_attempt(self):
        session = MagicMock()
        session.get.return_value = _make_response(200, b"image_data")

        result = download_image(session, "https://example.com/img.jpg")

        assert result == b"image_data"
        assert session.get.call_count == 1

    def test_retries_on_5xx_then_succeeds(self):
        session = MagicMock()
        fail_resp = _make_response(503)
        fail_resp.raise_for_status = MagicMock(
            side_effect=requests.HTTPError(response=fail_resp)
        )
        ok_resp = _make_response(200, b"ok")

        session.get.side_effect = [fail_resp, ok_resp]

        with patch("modules.multporn.sleep"):
            result = download_image(session, "https://example.com/img.jpg")

        assert result == b"ok"
        assert session.get.call_count == 2

    def test_no_retry_on_4xx(self):
        session = MagicMock()
        fail_resp = _make_response(404)
        fail_resp.raise_for_status = MagicMock(
            side_effect=requests.HTTPError(response=fail_resp)
        )
        session.get.return_value = fail_resp

        with patch("modules.multporn.sleep"):
            with pytest.raises(requests.HTTPError):
                download_image(session, "https://example.com/img.jpg")

        assert session.get.call_count == 1

    def test_fails_after_max_retries_on_5xx(self):
        session = MagicMock()
        fail_resp = _make_response(500)
        fail_resp.raise_for_status = MagicMock(
            side_effect=requests.HTTPError(response=fail_resp)
        )
        session.get.return_value = fail_resp

        with patch("modules.multporn.sleep"):
            with pytest.raises(requests.HTTPError):
                download_image(session, "https://example.com/img.jpg")

        assert session.get.call_count == MAX_DOWNLOAD_RETRIES

    def test_retries_on_connection_error(self):
        session = MagicMock()
        ok_resp = _make_response(200, b"data")
        session.get.side_effect = [
            requests.ConnectionError("refused"),
            ok_resp,
        ]

        with patch("modules.multporn.sleep"):
            result = download_image(session, "https://example.com/img.jpg")

        assert result == b"data"
        assert session.get.call_count == 2

    def test_retry_delay_called_between_attempts(self):
        session = MagicMock()
        fail_resp = _make_response(503)
        fail_resp.raise_for_status = MagicMock(
            side_effect=requests.HTTPError(response=fail_resp)
        )
        ok_resp = _make_response(200, b"ok")
        session.get.side_effect = [fail_resp, ok_resp]

        with patch("modules.multporn.sleep") as mock_sleep:
            download_image(session, "https://example.com/img.jpg")

        mock_sleep.assert_called_once_with(DOWNLOAD_RETRY_DELAY)
