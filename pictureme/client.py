"""HTTP client for the PictureME v3 API.

A thin wrapper around httpx.Client that knows the base URL + bearer token
once and lets command modules call .get/.post with relative paths. Errors
are normalized to APIError so callers don't have to catch httpx-specific
exceptions.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .config import CLIConfig


class APIError(Exception):
    """Raised when the backend returns a non-2xx status or fails to respond."""

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class PictureMEClient:
    def __init__(self, cfg: CLIConfig, timeout: float = 60.0):
        self.cfg = cfg
        headers: dict[str, str] = {"Accept": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self._client = httpx.Client(
            base_url=cfg.host,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PictureMEClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ----- generic verbs -----

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs)

    def request_with_status(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        """As the verb helpers, but also yielding the HTTP status.

        Callers that want to report the status of a successful response (the
        generic `admin api` surface) need it; the verb helpers stay
        status-free so existing call sites are unchanged.
        """
        return self._request_with_status(method, path, **kwargs)

    # ----- internals -----

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        _, payload = self._request_with_status(method, path, **kwargs)
        return payload

    def _request_with_status(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        try:
            kwargs["follow_redirects"] = False
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise APIError("network error") from None

        if 300 <= resp.status_code < 400:
            raise APIError(
                f"HTTP {resp.status_code}: redirects are disabled",
                status_code=resp.status_code,
            )

        # Try to decode JSON regardless of status so error bodies surface.
        payload: Any
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text

        if resp.status_code >= 400:
            # Server error bodies may echo passwords or bearer credentials.
            msg = f"HTTP {resp.status_code}"
            raise APIError(msg, status_code=resp.status_code, payload=payload)

        return resp.status_code, payload


def _extract_error_message(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    if isinstance(payload, str) and payload:
        return payload
    return None
