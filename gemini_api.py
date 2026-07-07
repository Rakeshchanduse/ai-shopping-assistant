"""
Async wrapper for the Google Gemini API using the new google-genai SDK.

Mirrors the GainsAPI interface so MCPClient can use either backend
by simply swapping the api object.

Package  : google-genai  (pip install google-genai)
Endpoint : Google Generative AI (gemini-2.0-flash by default)
Auth     : GOOGLE_API_KEY
Session  : Multi-turn history is managed via the SDK Chat object per session_id.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class GeminiAPIError(Exception):
    """Raised when the Gemini API returns an error."""


@dataclass
class GeminiResponse:
    """Structured wrapper around a Gemini API reply."""

    text: str
    session_id: str | None = None


class GeminiAPI:
    """Async Gemini client using the new google-genai SDK.

    Each session_id maps to a persistent async Chat object which carries
    the full conversation history automatically.
    A new UUID is generated when session_id is None (new conversation).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        timeout: float = 120.0,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self.model_name = model
        self.timeout = timeout
        self._chats: dict[str, Any] = {}  # session_id -> async Chat object

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _extract_text(response) -> str:
        """Safely extract text from a Gemini response.

        Walks through response.candidates -> parts and collects any text
        parts, returning an empty string if none are found.
        """
        # Fast path: the SDK property works fine
        try:
            t = response.text
            if t is not None:
                return t
        except (AttributeError, ValueError) as exc:
            logger.debug("response.text raised %s: %s", type(exc).__name__, exc)

        # Slow path: dig into candidates -> content -> parts
        text_parts: list[str] = []
        try:
            if not response.candidates:
                pf = getattr(response, "prompt_feedback", None)
                logger.warning("No candidates in Gemini response. prompt_feedback=%s", pf)
                return ""

            for ci, candidate in enumerate(response.candidates):
                finish_reason = getattr(candidate, "finish_reason", None)
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) or [] if content else []

                for pi, part in enumerate(parts):
                    has_text = hasattr(part, "text") and part.text is not None
                    has_fc = hasattr(part, "function_call") and part.function_call is not None
                    is_thought = getattr(part, "thought", False)
                    logger.debug(
                        "candidate[%d].part[%d]: has_text=%s is_thought=%s has_fc=%s finish=%s",
                        ci, pi, has_text, is_thought, has_fc, finish_reason,
                    )
                    # Collect non-thought text parts
                    if has_text and part.text and not is_thought:
                        text_parts.append(part.text)

        except (AttributeError, TypeError) as exc:
            logger.warning("Error walking Gemini response: %s", exc)

        if text_parts:
            return "\n".join(text_parts)

        logger.warning(
            "Gemini response contained no usable text parts. "
            "This may indicate the response was blocked by safety filters, "
            "the model returned only thinking tokens, or an API issue."
        )
        return ""

    # ── main entry point ─────────────────────────────────────────────────
    async def chat(
        self,
        query: str,
        session_id: str | None = None,
        context: str | None = None,
    ) -> GeminiResponse:
        """Send a chat query and return the response.

        Parameters
        ----------
        query : str
            The user / agent query text.
        session_id : str | None
            Optional session ID for multi-turn conversations.  When None, a
            new UUID is generated and returned so the caller can continue the
            session on the next call.
        context : str | None
            System instruction injected into the model (tool descriptions).
        """
        if not session_id or session_id not in self._chats:
            session_id = session_id or str(uuid.uuid4())
            config = types.GenerateContentConfig(
                system_instruction=context or None,
            )
            self._chats[session_id] = self._client.aio.chats.create(
                model=self.model_name,
                config=config,
            )

        logger.debug(
            "Gemini chat  session=%s  model=%s  query_len=%d",
            session_id,
            self.model_name,
            len(query),
        )

        try:
            response = await self._chats[session_id].send_message(query)
        except Exception as exc:
            raise GeminiAPIError(str(exc)) from exc

        text = self._extract_text(response)
        return GeminiResponse(text=text, session_id=session_id)

    # ── lifecycle ────────────────────────────────────────────────────────
    async def close(self) -> None:
        """No persistent HTTP connections to clean up."""
        pass
