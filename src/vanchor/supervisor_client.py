"""Stdlib-only synchronous client for the host-side vanchor-supervisor.

Called via asyncio.to_thread from the runtime to avoid blocking the event loop.
"""
from __future__ import annotations
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


class SupervisorClient:
    """Thin HTTP client for the supervisor's localhost-only /v1 API.

    Reads the authentication token lazily per request so the token file
    may be written by the supervisor after the app starts.
    """

    def __init__(self, url: str, token_file: str) -> None:
        self._url = url.rstrip("/")
        self._token_file = token_file

    def _token(self) -> str | None:
        p = Path(self._token_file)
        try:
            return p.read_text().strip() if p.exists() else None
        except OSError:
            return None

    def status(self, timeout: float = 3.0) -> dict | None:
        """Return the parsed /v1/status response, or None if unreachable."""
        try:
            _code, data = self.request("GET", "/v1/status", None, timeout=timeout)
            if isinstance(data, dict):
                return data
            return None
        except Exception:  # noqa: BLE001
            return None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        timeout: float = 15.0,
    ) -> tuple[int, dict | bytes]:
        """Generic /v1 passthrough.

        Returns (status_code, parsed_json_dict_or_raw_bytes).
        Raises URLError / OSError on connection problems.
        """
        url = self._url + path
        token = self._token()
        headers: dict[str, str] = {}
        if token:
            headers["X-Supervisor-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                code = resp.status
                # Try JSON first; fall back to raw bytes
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct or (raw and raw[:1] in (b"{", b"[")):
                    try:
                        return code, json.loads(raw)
                    except Exception:
                        pass
                return code, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, raw
