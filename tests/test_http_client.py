"""Exercise real Requests response parsing with a mocked, offline transport."""

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from data_factory.exceptions import ConfigurationError, DataError, DownloadError
from data_factory.http_client import HttpClient


def response(payload=None, *, status=200, headers=None, raw=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.encoding = "utf-8"
    result._content = raw if raw is not None else json.dumps(payload).encode("utf-8")
    result._content_consumed = True
    result.close = Mock(wraps=result.close)
    return result


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "timeout_seconds": 5,
            "max_retries": 2,
            "retry_backoff_seconds": 1,
            "max_retry_delay_seconds": 10,
        }
        self.url = "https://example.test/query"
        self.session = Mock(spec=requests.Session)
        self.client = HttpClient(self.settings, session=self.session)
        self.sleep_patch = patch("data_factory.http_client.time.sleep")
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_get_sends_query_and_timeout_without_body(self):
        result = response([{"unique_key": "1"}])
        self.session.request.return_value = result
        self.assertEqual(self.client.request_json("get", self.url, params={"$limit": 5}), [{"unique_key": "1"}])
        self.session.request.assert_called_once_with("GET", self.url, params={"$limit": 5}, timeout=5)
        result.close.assert_called_once()

    def test_post_sends_json_body_separately_from_query(self):
        self.session.request.return_value = response({"rows": [1]}, status=201)
        payload = {"query": "SELECT *", "limit": 2}
        headers = {"Accept": "application/json"}
        result = self.client.request_json("POST", self.url, params={"version": "2"}, json_body=payload, headers=headers)
        self.assertEqual(result, {"rows": [1]})
        self.session.request.assert_called_once_with(
            "POST", self.url, params={"version": "2"}, json=payload,
            headers=headers, timeout=5,
        )

    def test_post_sends_form_body(self):
        self.session.request.return_value = response({"ok": True})
        self.client.request_json("post", self.url, data={"query": "all"})
        self.session.request.assert_called_once_with("POST", self.url, params=None, data={"query": "all"}, timeout=5)

    def test_real_requests_serializes_post_json_without_network(self):
        # Keep Requests' preparation/serialization and replace only its adapter.
        adapter = Mock(spec=requests.adapters.BaseAdapter)
        adapter.send.return_value = response({"rows": []})
        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", adapter)
            with HttpClient(self.settings, session=session) as client:
                result = client.request_json(
                    "POST", self.url, params={"version": "2"},
                    json_body={"query": "read-only"},
                )
        self.assertEqual(result, {"rows": []})
        prepared = adapter.send.call_args.args[0]
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(parse_qs(urlsplit(prepared.url).query), {"version": ["2"]})
        self.assertEqual(json.loads(prepared.body), {"query": "read-only"})
        self.assertEqual(prepared.headers["Content-Type"], "application/json")
        self.assertEqual(adapter.send.call_args.kwargs["timeout"], 5)
        adapter.send.assert_called_once()

    def test_post_does_not_retry_by_default(self):
        cases = [requests.Timeout("read timed out"), response(status=503), response(raw=b"{partial")]
        for failure in cases:
            with self.subTest(failure=failure):
                self.session.request.reset_mock(side_effect=True)
                self.session.request.side_effect = [failure]
                with self.assertRaises((DownloadError, DataError)):
                    self.client.request_json("POST", self.url, json_body={"operation": "write"})
                self.session.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_read_only_post_can_explicitly_retry_with_identical_body(self):
        first = response(status=503)
        self.session.request.side_effect = [first, requests.Timeout("timeout"), response({"rows": []})]
        payload = {"query": "read-only"}
        result = self.client.request_json("POST", self.url, json_body=payload, retry_safe=True)
        self.assertEqual(result, {"rows": []})
        self.assertEqual(self.session.request.call_count, 3)
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[1], calls[2])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])
        first.close.assert_called_once()

    def test_get_retries_rate_limit_and_server_error(self):
        self.session.request.side_effect = [response(status=429, headers={"Retry-After": "3"}), response(status=500), response([])]
        self.assertEqual(self.client.request_json("GET", self.url), [])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [3, 2])

    def test_retry_after_is_capped_by_configuration(self):
        self.session.request.side_effect = [response(status=429, headers={"Retry-After": "3600"}), response([])]
        self.client.request_json("GET", self.url)
        self.sleep.assert_called_once_with(10)

    @patch("data_factory.http_client.time.time", return_value=0)
    def test_retry_after_http_date(self, now):
        self.session.request.side_effect = [response(status=503, headers={"Retry-After": "Thu, 01 Jan 1970 00:00:04 GMT"}), response([])]
        self.client.request_json("GET", self.url)
        self.sleep.assert_called_once_with(4)

    def test_invalid_retry_after_uses_backoff(self):
        for value in ["invalid", "NaN", "Infinity"]:
            with self.subTest(value=value):
                self.sleep.reset_mock()
                self.session.request.side_effect = [response(status=429, headers={"Retry-After": value}), response([])]
                self.client.request_json("GET", self.url)
                self.sleep.assert_called_once_with(1)

    def test_backoff_is_configurable_and_capped(self):
        settings = {**self.settings, "retry_backoff_seconds": 3, "max_retry_delay_seconds": 4}
        self.session.request.side_effect = [requests.ConnectionError("offline"), requests.Timeout("timeout"), response([])]
        HttpClient(settings, session=self.session).request_json("GET", self.url)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [3, 4])

    def test_nonretryable_status_fails_immediately(self):
        for status in [301, 400, 401, 403, 404, 405]:
            with self.subTest(status=status):
                self.session.request.reset_mock(side_effect=True)
                result = response(status=status)
                self.session.request.return_value = result
                with self.assertRaisesRegex(DownloadError, str(status)):
                    self.client.request_json("GET", self.url)
                self.session.request.assert_called_once()
                result.close.assert_called_once()
        self.sleep.assert_not_called()

    def test_network_retry_exhaustion_is_download_error(self):
        self.session.request.side_effect = [requests.ConnectionError("offline")] * 3
        with self.assertRaisesRegex(DownloadError, "3 attempt"):
            self.client.request_json("GET", self.url)
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_malformed_json_can_recover_on_retry(self):
        first = response(raw=b"<html>not JSON</html>")
        self.session.request.side_effect = [first, response({"ok": True})]
        self.assertEqual(self.client.request_json("GET", self.url), {"ok": True})
        self.sleep.assert_called_once_with(1)
        first.close.assert_called_once()

    def test_malformed_json_exhaustion_is_data_error(self):
        self.session.request.side_effect = [response(raw=b"{truncated") for _ in range(3)]
        with self.assertRaises(DataError):
            self.client.request_json("GET", self.url)
        self.assertEqual(self.session.request.call_count, 3)

    def test_zero_retries_makes_exactly_one_attempt(self):
        self.session.request.side_effect = requests.Timeout("timeout")
        client = HttpClient({**self.settings, "max_retries": 0}, session=self.session)
        with self.assertRaises(DownloadError):
            client.request_json("GET", self.url)
        self.session.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_invalid_method_url_and_body_fail_before_http(self):
        cases = [
            ("DELETE", self.url, {}), (None, self.url, {}),
            ("GET", "relative/path", {}), ("GET", None, {}),
            ("POST", self.url, {"json_body": {}, "data": {}}),
            ("POST", self.url, {"retry_safe": "yes"}),
        ]
        for method, url, kwargs in cases:
            with self.subTest(method=method, url=url, kwargs=kwargs):
                with self.assertRaises(ConfigurationError):
                    self.client.request_json(method, url, **kwargs)
        self.session.request.assert_not_called()

    def test_invalid_settings_fail_before_session_creation(self):
        cases = [None, {}, {**self.settings, "max_retries": True}]
        for key in ["timeout_seconds", "retry_backoff_seconds", "max_retry_delay_seconds"]:
            for value in [float("nan"), float("inf"), -1, True, "5"]:
                cases.append({**self.settings, key: value})
        cases.append({**self.settings, "timeout_seconds": 0})
        with patch("data_factory.http_client.requests.Session") as factory:
            for settings in cases:
                with self.subTest(settings=settings):
                    with self.assertRaises(ConfigurationError):
                        HttpClient(settings)
            factory.assert_not_called()

    def test_injected_session_remains_open_after_context(self):
        self.session.request.return_value = response([])
        with self.client as client:
            client.request_json("GET", self.url)
        self.session.close.assert_not_called()

    def test_owned_session_closes_when_a_request_fails(self):
        self.session.request.side_effect = requests.Timeout("timeout")
        with patch("data_factory.http_client.requests.Session", return_value=self.session):
            with self.assertRaises(DownloadError):
                with HttpClient({**self.settings, "max_retries": 0}) as client:
                    client.request_json("GET", self.url)
        self.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
