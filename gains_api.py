"""
Async wrapper for the Dover Gains AI API.

Endpoint : POST {base_url}/chat
Auth     : Bearer token
Payload  : multipart form-data – query, sessionId (opt), context (opt), image (opt)
Response : plain-text body, sessionId returned in response headers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GainsAPIError(Exception):
    """Raised when the Gains API returns a non-2xx status."""


@dataclass
class GainsResponse:
    """Structured wrapper around a Gains API reply."""
    text: str
    session_id: str | None = None
    raw_headers: dict[str, str] = field(default_factory=dict)


class GainsAPI:
    """Thin async client for the Gains /chat endpoint."""

    def __init__(
        self,
        base_url: str = "https://gains.dovercorp.com/se/gains-api",
        token: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.AsyncClient(timeout=timeout, verify=False)

    # ── helpers ──────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # ── main entry point ─────────────────────────────────────────────────
    async def chat(
        self,
        query: str,
        session_id: str | None = None,
        context: str | None = None,
    ) -> GainsResponse:
        """Send a chat query and return the response.

        Parameters
        ----------
        query : str
            The user / agent query text.
        session_id : str | None
            Optional session ID for multi-turn conversations.
        context : str | None
            Optional context string injected into the request.
        """
        data: dict[str, str] = {"query": query}
        if session_id:
            data["sessionId"] = session_id
        if context:
            data["context"] = context

        url = f"{self.base_url}/chat"
        logger.debug("POST %s  data_keys=%s", url, list(data.keys()))

        response = await self.client.post(
            url,
            headers=self._headers(),
            data=data,          # multipart form-data
            timeout=120.0,
        )

        if response.status_code != 200:
            body = response.text[:500]
            raise GainsAPIError(
                f"Gains API returned {response.status_code}: {body}"
            )

        return GainsResponse(
            text=response.text,
            session_id=response.headers.get("sessionId") or response.headers.get("sessionid"),
            raw_headers=dict(response.headers),
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self.client.aclose()
