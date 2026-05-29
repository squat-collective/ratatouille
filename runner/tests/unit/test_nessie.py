"""Tests for nessie — Nessie v2 REST client for branch operations."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from rat_runner.config import NessieConfig
from rat_runner.nessie import (
    _is_transient_error,
    _validate_branch_name,
    create_branch,
    delete_branch,
    merge_branch,
    retry_on_transient,
)


def _nessie() -> NessieConfig:
    return NessieConfig(url="http://nessie:19120/api/v1")


class TestCreateBranch:
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_creates_branch_from_main(self, mock_urlopen: MagicMock):
        # First call: _get_reference (GET main) — Nessie 0.99.x wraps the
        # reference under a top-level "reference" key.
        ref_response = MagicMock()
        ref_response.read.return_value = json.dumps(
            {"reference": {"name": "main", "hash": "abc123", "type": "BRANCH"}}
        ).encode()
        ref_response.__enter__ = lambda s: s
        ref_response.__exit__ = MagicMock(return_value=False)

        # Second call: create branch (POST /trees) — also wrapped.
        create_response = MagicMock()
        create_response.read.return_value = json.dumps(
            {"reference": {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}}
        ).encode()
        create_response.__enter__ = lambda s: s
        create_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [ref_response, create_response]

        result = create_branch(_nessie(), "run-r1")
        assert result == "abc123"
        assert mock_urlopen.call_count == 2


class TestMergeBranch:
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merges_source_to_target(self, mock_urlopen: MagicMock):
        # _get_reference (source: run-r1)
        source_ref_response = MagicMock()
        source_ref_response.read.return_value = json.dumps(
            {"reference": {"name": "run-r1", "hash": "def456", "type": "BRANCH"}}
        ).encode()
        source_ref_response.__enter__ = lambda s: s
        source_ref_response.__exit__ = MagicMock(return_value=False)

        # _get_reference (target: main) — needed for path-embedded @hash
        target_ref_response = MagicMock()
        target_ref_response.read.return_value = json.dumps(
            {"reference": {"name": "main", "hash": "main123", "type": "BRANCH"}}
        ).encode()
        target_ref_response.__enter__ = lambda s: s
        target_ref_response.__exit__ = MagicMock(return_value=False)

        # merge
        merge_response = MagicMock()
        merge_response.read.return_value = b"{}"
        merge_response.__enter__ = lambda s: s
        merge_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [source_ref_response, target_ref_response, merge_response]

        merge_branch(_nessie(), "run-r1", target="main")  # should not raise
        assert mock_urlopen.call_count == 3


class TestDeleteBranch:
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_deletes_existing_branch(self, mock_urlopen: MagicMock):
        # _get_reference
        ref_response = MagicMock()
        ref_response.read.return_value = json.dumps(
            {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}
        ).encode()
        ref_response.__enter__ = lambda s: s
        ref_response.__exit__ = MagicMock(return_value=False)

        # DELETE
        delete_response = MagicMock()
        delete_response.read.return_value = b""
        delete_response.__enter__ = lambda s: s
        delete_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [ref_response, delete_response]

        delete_branch(_nessie(), "run-r1")  # should not raise

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_delete_embeds_expected_hash_in_ref_path(self, mock_urlopen: MagicMock):
        # Regression: Nessie 0.99.x rejects the `?expected-hash=` query form
        # with 400; the hash must ride in the ref path via `{ref}@{hash}`.
        ref_response = MagicMock()
        ref_response.read.return_value = json.dumps(
            {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}
        ).encode()
        ref_response.__enter__ = lambda s: s
        ref_response.__exit__ = MagicMock(return_value=False)

        delete_response = MagicMock()
        delete_response.read.return_value = b""
        delete_response.__enter__ = lambda s: s
        delete_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [ref_response, delete_response]

        delete_branch(_nessie(), "run-r1")

        delete_url = mock_urlopen.call_args_list[1][0][0].full_url
        # @ encodes to %40 via urllib.parse.quote(safe='')
        assert "/trees/run-r1%40abc123" in delete_url
        assert "expected-hash" not in delete_url

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_ignores_404_on_get_reference(self, mock_urlopen: MagicMock):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,  # type: ignore[arg-type]
        )

        delete_branch(_nessie(), "nonexistent")  # should not raise

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_ignores_404_on_delete(self, mock_urlopen: MagicMock):
        # _get_reference succeeds
        ref_response = MagicMock()
        ref_response.read.return_value = json.dumps(
            {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}
        ).encode()
        ref_response.__enter__ = lambda s: s
        ref_response.__exit__ = MagicMock(return_value=False)

        # DELETE returns 404
        mock_urlopen.side_effect = [
            ref_response,
            urllib.error.HTTPError(
                url="",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,  # type: ignore[arg-type]
            ),
        ]

        delete_branch(_nessie(), "run-r1")  # should not raise


class TestBranchNameValidation:
    def test_valid_branch_names(self):
        assert _validate_branch_name("run-r1") == "run-r1"
        assert _validate_branch_name("main") == "main"
        assert _validate_branch_name("feature.branch-123") == "feature.branch-123"

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="Invalid Nessie branch name"):
            _validate_branch_name("")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError):
            _validate_branch_name("../../etc/passwd")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="Invalid Nessie branch name"):
            _validate_branch_name("my branch")

    def test_rejects_slashes(self):
        with pytest.raises(ValueError, match="Invalid Nessie branch name"):
            _validate_branch_name("feat/test")

    def test_rejects_special_characters(self):
        for name in ["; rm -rf /", "&& cat /etc/passwd", "$(whoami)", "|ls"]:
            with pytest.raises(ValueError):
                _validate_branch_name(name)

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_create_branch_rejects_invalid_name(self, mock_urlopen: MagicMock):
        with pytest.raises(ValueError):
            create_branch(_nessie(), "../escape")

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_branch_url_encodes_target(self, mock_urlopen: MagicMock):
        source_ref_response = MagicMock()
        source_ref_response.read.return_value = json.dumps(
            {"reference": {"name": "run-r1", "hash": "def456", "type": "BRANCH"}}
        ).encode()
        source_ref_response.__enter__ = lambda s: s
        source_ref_response.__exit__ = MagicMock(return_value=False)

        target_ref_response = MagicMock()
        target_ref_response.read.return_value = json.dumps(
            {"reference": {"name": "main", "hash": "main123", "type": "BRANCH"}}
        ).encode()
        target_ref_response.__enter__ = lambda s: s
        target_ref_response.__exit__ = MagicMock(return_value=False)

        merge_response = MagicMock()
        merge_response.read.return_value = b"{}"
        merge_response.__enter__ = lambda s: s
        merge_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [source_ref_response, target_ref_response, merge_response]

        merge_branch(_nessie(), "run-r1", target="main")

        # Verify the merge URL was called with a properly encoded target,
        # including the path-embedded @hash expected-hash suffix.
        merge_call = mock_urlopen.call_args_list[2]
        merge_url = merge_call[0][0].full_url
        # @ encodes to %40 via urllib.parse.quote(safe='')
        assert "/trees/main%40main123/history/merge" in merge_url


def _http_error(code: int, msg: str = "Error") -> urllib.error.HTTPError:
    """Helper to create an HTTPError with less boilerplate."""
    return urllib.error.HTTPError(
        url="http://nessie:19120/api/v2/trees/main",
        code=code,
        msg=msg,
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def _url_error(reason: str = "Connection refused") -> urllib.error.URLError:
    """Helper to create a URLError (connection-level failure)."""
    return urllib.error.URLError(reason=reason)


def _ok_response(data: dict[str, str] | None = None) -> MagicMock:
    """Helper to create a successful urllib response mock."""
    if data is None:
        data = {"name": "main", "hash": "abc123", "type": "BRANCH"}
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestIsTransientError:
    def test_5xx_is_transient(self):
        assert _is_transient_error(_http_error(500)) is True
        assert _is_transient_error(_http_error(502)) is True
        assert _is_transient_error(_http_error(503)) is True

    def test_4xx_is_not_transient(self):
        assert _is_transient_error(_http_error(400)) is False
        assert _is_transient_error(_http_error(404)) is False
        assert _is_transient_error(_http_error(409)) is False
        assert _is_transient_error(_http_error(429)) is False

    def test_url_error_is_transient(self):
        assert _is_transient_error(_url_error("Connection refused")) is True
        assert _is_transient_error(_url_error("[Errno -2] Name or service not known")) is True

    def test_timeout_error_is_transient(self):
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_value_error_is_not_transient(self):
        assert _is_transient_error(ValueError("bad value")) is False

    def test_runtime_error_is_not_transient(self):
        assert _is_transient_error(RuntimeError("boom")) is False


class TestRetryOnTransient:
    def test_succeeds_on_first_attempt(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        assert fn() == "ok"
        assert call_count == 1
        mock_sleep.assert_not_called()

    def test_retries_on_5xx_and_succeeds(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise _http_error(503, "Service Unavailable")
            return "ok"

        assert fn() == "ok"
        assert call_count == 3
        # Two sleeps: 0.5s (attempt 0), 1.0s (attempt 1)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.5), call(1.0)])

    def test_retries_on_url_error_and_succeeds(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _url_error("Connection refused")
            return "ok"

        assert fn() == "ok"
        assert call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    def test_raises_after_all_retries_exhausted(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            raise _http_error(503, "Service Unavailable")

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            fn()

        assert exc_info.value.code == 503
        assert call_count == 4  # 1 initial + 3 retries
        # Three sleeps: 0.5s, 1.0s, 2.0s
        assert mock_sleep.call_count == 3
        mock_sleep.assert_has_calls([call(0.5), call(1.0), call(2.0)])

    def test_does_not_retry_on_4xx(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            raise _http_error(404, "Not Found")

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            fn()

        assert exc_info.value.code == 404
        assert call_count == 1
        mock_sleep.assert_not_called()

    def test_does_not_retry_on_value_error(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=3, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            fn()

        assert call_count == 1
        mock_sleep.assert_not_called()

    def test_exponential_backoff_timing(self):
        """Verify backoff doubles each attempt: 0.5, 1.0, 2.0, 4.0."""
        mock_sleep = MagicMock()

        @retry_on_transient(max_retries=4, initial_backoff=0.5, _sleep=mock_sleep)
        def fn() -> str:
            raise _http_error(500)

        with pytest.raises(urllib.error.HTTPError):
            fn()

        assert mock_sleep.call_count == 4
        mock_sleep.assert_has_calls([call(0.5), call(1.0), call(2.0), call(4.0)])

    def test_custom_backoff_and_retries(self):
        mock_sleep = MagicMock()
        call_count = 0

        @retry_on_transient(max_retries=2, initial_backoff=1.0, _sleep=mock_sleep)
        def fn() -> str:
            nonlocal call_count
            call_count += 1
            raise _http_error(502)

        with pytest.raises(urllib.error.HTTPError):
            fn()

        assert call_count == 3  # 1 initial + 2 retries
        mock_sleep.assert_has_calls([call(1.0), call(2.0)])

    def test_preserves_function_name(self):
        mock_sleep = MagicMock()

        @retry_on_transient(_sleep=mock_sleep)
        def my_function() -> str:
            return "ok"

        assert my_function.__name__ == "my_function"


class TestGetReferenceRetry:
    """Test that _get_reference retries on transient errors."""

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_get_reference_retries_on_503(self, mock_urlopen: MagicMock, mock_sleep: MagicMock):
        """_get_reference retries on 503 and succeeds on second attempt."""
        ok_resp = _ok_response()
        mock_urlopen.side_effect = [
            _http_error(503, "Service Unavailable"),
            ok_resp,
        ]

        # Import _get_reference directly to test it
        from rat_runner.nessie import _get_reference

        result = _get_reference(_nessie(), "main")
        assert result["hash"] == "abc123"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_get_reference_does_not_retry_on_404(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ):
        """_get_reference does NOT retry 404 — it's a client error, not transient."""
        mock_urlopen.side_effect = _http_error(404, "Not Found")

        from rat_runner.nessie import _get_reference

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get_reference(_nessie(), "nonexistent")

        assert exc_info.value.code == 404
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_get_reference_retries_on_connection_error(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ):
        """_get_reference retries on connection errors (URLError)."""
        ok_resp = _ok_response()
        mock_urlopen.side_effect = [
            _url_error("Connection refused"),
            ok_resp,
        ]

        from rat_runner.nessie import _get_reference

        result = _get_reference(_nessie(), "main")
        assert result["hash"] == "abc123"
        assert mock_urlopen.call_count == 2


class TestCreateBranchRetry:
    """Test that create_branch retries on transient errors."""

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_create_branch_retries_on_502(self, mock_urlopen: MagicMock, mock_sleep: MagicMock):
        """create_branch retries the whole operation when the POST returns 502."""
        # First attempt: _get_reference succeeds, POST fails with 502
        ref_resp_1 = _ok_response()
        # Second attempt (retry): _get_reference succeeds, POST succeeds
        ref_resp_2 = _ok_response()
        # Create response must be wrapped — create_branch reads
        # result["reference"]["hash"] per Nessie 0.99.x.
        create_resp = _ok_response(
            {"reference": {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}}
        )

        mock_urlopen.side_effect = [
            ref_resp_1,  # _get_reference (1st attempt)
            _http_error(502, "Bad Gateway"),  # POST /trees (1st attempt fails)
            ref_resp_2,  # _get_reference (2nd attempt, retry)
            create_resp,  # POST /trees (2nd attempt succeeds)
        ]

        result = create_branch(_nessie(), "run-r1")
        assert result == "abc123"
        assert mock_urlopen.call_count == 4


class TestMergeBranchRetry:
    """Test that merge_branch retries on transient errors."""

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_branch_retries_on_500(self, mock_urlopen: MagicMock, mock_sleep: MagicMock):
        """merge_branch retries when the merge POST returns 500."""
        ref_resp_1 = _ok_response({"name": "run-r1", "hash": "def456", "type": "BRANCH"})
        ref_resp_2 = _ok_response({"name": "run-r1", "hash": "def456", "type": "BRANCH"})
        merge_resp = MagicMock()
        merge_resp.read.return_value = b"{}"
        merge_resp.__enter__ = lambda s: s
        merge_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [
            ref_resp_1,  # _get_reference (1st attempt)
            _http_error(500, "Internal Server Error"),  # POST merge (1st attempt fails)
            ref_resp_2,  # _get_reference (2nd attempt, retry)
            merge_resp,  # POST merge (2nd attempt succeeds)
        ]

        merge_branch(_nessie(), "run-r1", target="main")  # should not raise
        assert mock_urlopen.call_count == 4


class TestMergeBranchConflictRetry:
    """In-function retry of 409 CONFLICT on the merge POST (concurrent
    merge moved target). merge_branch should refetch the target hash and
    retry up to MERGE_CONFLICT_MAX_RETRIES times before giving up.
    """

    @staticmethod
    def _src_ref(hash_: str = "src0") -> MagicMock:
        return _ok_response({"reference": {"name": "run-r1", "hash": hash_, "type": "BRANCH"}})

    @staticmethod
    def _tgt_ref(hash_: str) -> MagicMock:
        return _ok_response({"reference": {"name": "main", "hash": hash_, "type": "BRANCH"}})

    @staticmethod
    def _merge_ok() -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_conflict_409_refetches_target_hash_and_retries(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ):
        """A 409 on the merge POST triggers an internal refetch of the
        target hash and a re-post — no outer-decorator backoff sleeps."""
        # Attempt 1:
        #   _get_reference(source) → src
        #   _get_reference(target) → hashA
        #   POST merge → 409 CONFLICT
        # Attempt 2:
        #   _get_reference(target) → hashB  (NOTE: refetch — source not re-fetched)
        #   POST merge → 200 OK
        mock_urlopen.side_effect = [
            self._src_ref("src0"),
            self._tgt_ref("hashA"),
            _http_error(409, "Conflict"),
            self._tgt_ref("hashB"),
            self._merge_ok(),
        ]

        merge_branch(_nessie(), "run-r1", target="main")

        # 5 urlopen calls: src + 2× target + 2× merge POST.
        assert mock_urlopen.call_count == 5
        # The transient-retry backoff sleep MUST NOT fire — 409 is handled
        # in-function, not by the @retry_on_transient decorator.
        mock_sleep.assert_not_called()

        # Confirm the SECOND merge POST URL used the fresh target hash (hashB).
        merge_calls = [
            c for c in mock_urlopen.call_args_list if "/history/merge" in c[0][0].full_url
        ]
        assert len(merge_calls) == 2
        assert "main%40hashA" in merge_calls[0][0][0].full_url
        assert "main%40hashB" in merge_calls[1][0][0].full_url

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_conflict_409_exhausts_retries_and_raises(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ):
        """When every attempt 409s, merge_branch raises HTTPError(409)."""
        # 3 attempts. Each attempt: 1 target GET + 1 merge POST = 2 calls.
        # Plus the initial source GET = 1 + 3×2 = 7 calls total.
        mock_urlopen.side_effect = [
            self._src_ref(),
            self._tgt_ref("hashA"),
            _http_error(409, "Conflict"),
            self._tgt_ref("hashB"),
            _http_error(409, "Conflict"),
            self._tgt_ref("hashC"),
            _http_error(409, "Conflict"),
        ]

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            merge_branch(_nessie(), "run-r1", target="main")
        assert exc_info.value.code == 409
        # 3 in-function attempts, no transient-decorator retries.
        assert mock_urlopen.call_count == 7
        mock_sleep.assert_not_called()

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_non_409_4xx_raises_immediately_no_refetch(
        self, mock_urlopen: MagicMock, mock_sleep: MagicMock
    ):
        """A 400 on the merge POST is permanent and raised on the first
        attempt — no in-function refetch, no decorator retry."""
        mock_urlopen.side_effect = [
            self._src_ref(),
            self._tgt_ref("hashA"),
            _http_error(400, "Bad Request"),
        ]

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            merge_branch(_nessie(), "run-r1", target="main")
        assert exc_info.value.code == 400
        assert mock_urlopen.call_count == 3
        mock_sleep.assert_not_called()


class TestUrlopenTimeoutParameter:
    """Verify that every urlopen call in nessie.py passes timeout=10."""

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_get_reference_passes_timeout(self, mock_urlopen: MagicMock):
        """_get_reference should call urlopen with timeout=10."""
        mock_urlopen.return_value = _ok_response()

        from rat_runner.nessie import _get_reference

        _get_reference(_nessie(), "main")

        assert mock_urlopen.call_count == 1
        _, kwargs = mock_urlopen.call_args
        assert kwargs.get("timeout") == 10

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_create_branch_passes_timeout_on_all_calls(self, mock_urlopen: MagicMock):
        """create_branch should pass timeout=10 on both _get_reference and POST."""
        ref_resp = _ok_response()
        # Wrapped shape per Nessie 0.99.x.
        create_resp = _ok_response(
            {"reference": {"name": "run-r1", "hash": "abc123", "type": "BRANCH"}}
        )
        mock_urlopen.side_effect = [ref_resp, create_resp]

        create_branch(_nessie(), "run-r1")

        assert mock_urlopen.call_count == 2
        for call_obj in mock_urlopen.call_args_list:
            _, kwargs = call_obj
            assert kwargs.get("timeout") == 10

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_merge_branch_passes_timeout_on_all_calls(self, mock_urlopen: MagicMock):
        """merge_branch should pass timeout on all three urlopen calls.

        Nessie 0.99.x requires GET source ref, GET target ref (for the
        @hash expected-hash), and POST merge — three calls total.
        """
        source_ref_resp = _ok_response(
            {"reference": {"name": "run-r1", "hash": "def456", "type": "BRANCH"}}
        )
        target_ref_resp = _ok_response(
            {"reference": {"name": "main", "hash": "main123", "type": "BRANCH"}}
        )
        merge_resp = MagicMock()
        merge_resp.read.return_value = b"{}"
        merge_resp.__enter__ = lambda s: s
        merge_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [source_ref_resp, target_ref_resp, merge_resp]

        merge_branch(_nessie(), "run-r1", target="main")

        assert mock_urlopen.call_count == 3
        for call_obj in mock_urlopen.call_args_list:
            _, kwargs = call_obj
            assert kwargs.get("timeout") in (10, 30)

    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_delete_branch_passes_timeout_on_all_calls(self, mock_urlopen: MagicMock):
        """delete_branch should pass timeout=10 on both _get_reference and DELETE."""
        ref_resp = _ok_response({"name": "run-r1", "hash": "abc123", "type": "BRANCH"})
        delete_resp = MagicMock()
        delete_resp.read.return_value = b""
        delete_resp.__enter__ = lambda s: s
        delete_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [ref_resp, delete_resp]

        delete_branch(_nessie(), "run-r1")

        assert mock_urlopen.call_count == 2
        for call_obj in mock_urlopen.call_args_list:
            _, kwargs = call_obj
            assert kwargs.get("timeout") == 10

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_timeout_error_triggers_retry(self, mock_urlopen: MagicMock, mock_sleep: MagicMock):
        """TimeoutError from urlopen should trigger retry via @retry_on_transient."""
        ok_resp = _ok_response()
        mock_urlopen.side_effect = [
            TimeoutError("timed out"),
            ok_resp,
        ]

        from rat_runner.nessie import _get_reference

        result = _get_reference(_nessie(), "main")
        assert result["hash"] == "abc123"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @patch("rat_runner.nessie.time.sleep")
    @patch("rat_runner.nessie.urllib.request.urlopen")
    def test_timeout_error_exhausts_retries(self, mock_urlopen: MagicMock, mock_sleep: MagicMock):
        """Persistent TimeoutError should exhaust retries and raise."""
        mock_urlopen.side_effect = TimeoutError("timed out")

        from rat_runner.nessie import _get_reference

        with pytest.raises(TimeoutError, match="timed out"):
            _get_reference(_nessie(), "main")

        # 1 initial + 3 retries = 4 attempts
        assert mock_urlopen.call_count == 4
        assert mock_sleep.call_count == 3
