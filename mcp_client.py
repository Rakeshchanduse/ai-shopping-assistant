"""
MCP Client – bridges the MCP server (e-commerce tools) with an LLM backend.

Supported backends: Gains AI (gains_api.py) and Google Gemini (gemini_api.py).
Because neither backend has native function-calling wired to MCP tools, we use
*prompt engineering* to let the LLM request tool calls:

    1. A system context describes available tools and the calling convention.
    2. When the LLM wants a tool it emits:   TOOL_CALL: {"tool":"…","args":{…}}
    3. We parse that line, execute the tool on the MCP server, and send a
       follow-up query containing the tool result.
    4. Repeat until the LLM responds with plain text (no TOOL_CALL).
"""

from __future__ import annotations

import os
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Matches a TOOL_CALL on a single line (no DOTALL so '.' won't cross lines)
_TOOL_CALL_LINE_RE = re.compile(r"TOOL_CALL:\s*(\{.+\})")

MAX_TOOL_ROUNDS = 10  # safety cap to prevent infinite loops


class _ServerConnection:
    """Holds the connection state for a single MCP server."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.session: ClientSession | None = None
        self.stdio_ctx: Any = None
        self.session_ctx: Any = None

    async def connect(self, server_script: str) -> list:
        """Start one MCP server and return its raw tool list."""
        params = StdioServerParameters(
            command=sys.executable,
            args=[server_script],
            env=os.environ.copy(),
        )
        self.stdio_ctx = stdio_client(params)
        read_stream, write_stream = await self.stdio_ctx.__aenter__()

        self.session_ctx = ClientSession(read_stream, write_stream)
        self.session = await self.session_ctx.__aenter__()
        await self.session.initialize()

        tools_result = await self.session.list_tools()
        logger.info("Server '%s' connected – %d tools", self.name, len(tools_result.tools))
        return tools_result.tools

    async def disconnect(self) -> None:
        if self.session_ctx:
            await self.session_ctx.__aexit__(None, None, None)
        if self.stdio_ctx:
            await self.stdio_ctx.__aexit__(None, None, None)


class MCPClient:
    """Orchestrates MCP server(s) and an LLM backend (Gains or Gemini)."""

    def __init__(self, llm_api) -> None:
        self.llm_api = llm_api
        self.tools: list[dict[str, Any]] = []       # all tool descriptions (merged)
        self._tool_names: set[str] = set()
        self._tool_to_session: dict[str, ClientSession] = {}  # tool -> session
        self._tool_to_server: dict[str, str] = {}              # tool -> server name
        self._server_tools: dict[str, list[dict[str, Any]]] = {}  # server -> tools
        self._servers: list[_ServerConnection] = []
        self.active_servers: set[str] = set()                  # user-selectable
        self.session_id: str | None = None                     # LLM session continuity

    # ── connection ───────────────────────────────────────────────────────
    async def connect(self, server_scripts: str | list[str]) -> None:
        """Start one or more MCP servers and merge their tool lists.

        Parameters
        ----------
        server_scripts : str | list[str]
            Path(s) to MCP server script(s).  A single string is also accepted
            for backward compatibility.
        """
        if isinstance(server_scripts, str):
            server_scripts = [server_scripts]

        for script in server_scripts:
            name = Path(script).stem
            conn = _ServerConnection(name)
            raw_tools = await conn.connect(script)
            self._servers.append(conn)

            # Register tools and map each tool name -> session / server
            described = self._describe_tools(raw_tools)
            self._server_tools[name] = described
            for t in described:
                if t["name"] not in self._tool_names:
                    self.tools.append(t)
                    self._tool_names.add(t["name"])
                    self._tool_to_session[t["name"]] = conn.session  # type: ignore[assignment]
                    self._tool_to_server[t["name"]] = name
                else:
                    logger.warning("Duplicate tool '%s' from server '%s' – skipped", t["name"], name)

            self.active_servers.add(name)

        logger.info("Total tools available: %d (from %d servers)", len(self.tools), len(self._servers))

    # ── tool helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _describe_tools(mcp_tools: list) -> list[dict[str, Any]]:
        """Convert MCP tool objects to simple dicts for display & prompting."""
        result: list[dict[str, Any]] = []
        for t in mcp_tools:
            params = t.inputSchema if t.inputSchema else {"type": "object", "properties": {}}
            result.append(
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": params,
                }
            )
        return result

    # ── server selection ────────────────────────────────────────────────
    def get_server_names(self) -> list[str]:
        """Return all connected server names."""
        return list(self._server_tools.keys())

    def get_active_tools(self) -> list[dict[str, Any]]:
        """Return only tools from currently active servers."""
        return [
            t for t in self.tools
            if self._tool_to_server.get(t["name"]) in self.active_servers
        ]

    def build_tool_context(self, tools: list[dict[str, Any]] | None = None) -> str:
        """Build an ultra-compact context string that teaches the LLM how to call tools."""
        tools = tools or self.tools
        lines = [
            'To use a tool reply: TOOL_CALL: {"tool":"<name>","args":{...}}',
            "One TOOL_CALL per reply. Tools:",
        ]
        for t in tools:
            props = t["parameters"].get("properties", {})
            required = t["parameters"].get("required", [])
            params = ",".join(
                f"{p}{'*' if p in required else ''}" for p in props
            )
            lines.append(f"{t['name']}({params}) - {t['description'][:80]}")
        
        # Prepend the global system prompt to the tool instructions
        return SYSTEM_PROMPT + "\n\n" + "\n".join(lines)

    # ── chat loop ────────────────────────────────────────────────────────
    async def chat(
        self,
        user_message: str,
        on_status: Any = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Send *user_message*, handle tool calls, return (final_reply, tool_call_log).

        The Gains session is maintained automatically via session_id.
        *on_status* is an optional callable(str) to report progress to the UI.
        """
        tool_call_log: list[dict[str, Any]] = []
        active_tools = self.get_active_tools()
        context = self.build_tool_context(active_tools)
        logger.info("Sending %d/%d tools in context", len(active_tools), len(self.tools))

        def _status(msg: str) -> None:
            if on_status:
                on_status(msg)

        async def _send(query: str, ctx: str | None = None) -> str:
            """Send to LLM with automatic retry (drop context on 500)."""
            try:
                resp = await self.llm_api.chat(
                    query=query, session_id=self.session_id, context=ctx,
                )
            except Exception as exc:
                if "500" in str(exc) and ctx:
                    _status("Retrying without tool context ...")
                    logger.warning("500 from API – retrying without context")
                    resp = await self.llm_api.chat(
                        query=query, session_id=self.session_id, context=None,
                    )
                else:
                    raise
            self.session_id = resp.session_id or self.session_id
            if resp.text is None:
                logger.warning("LLM backend returned None text; treating as empty response")
                return ""
            if not isinstance(resp.text, str):
                logger.warning(
                    "LLM backend returned non-string text (%s); coercing to string",
                    type(resp.text).__name__,
                )
                return str(resp.text)
            return resp.text

        # First request — send the user message
        _status("Sending query to LLM ...")
        reply_text = await _send(user_message, context)

        # Tool-call resolution loop
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            tc = self._parse_tool_call(reply_text)
            if tc is None:
                break  # no tool call -> we have the final answer
            rounds += 1

            tool_name = tc["tool"]
            tool_args = tc.get("args", {})

            _status(f"Running tool: {tool_name} ...")
            if tool_name not in self._tool_names:
                tool_result = f"Error: Unknown tool '{tool_name}'."
            else:
                logger.info("Tool call: %s(%s)", tool_name, tool_args)
                session = self._tool_to_session[tool_name]
                result = await session.call_tool(tool_name, tool_args)
                tool_result = "".join(
                    block.text for block in result.content if hasattr(block, "text")
                )

            tool_call_log.append(
                {"tool": tool_name, "args": tool_args, "result": tool_result}
            )

            # Feed the tool result back to the LLM
            _status("Sending tool result to LLM ...")
            follow_up = (
                f"Tool '{tool_name}' returned:\n"
                f"```\n{tool_result[:3000]}\n```\n"
                "Now provide your answer or call another tool if needed."
            )
            reply_text = await _send(follow_up, context)

        # Strip any leftover TOOL_CALL lines from the final answer
        clean_reply = self._strip_tool_call_lines(reply_text)
        return clean_reply, tool_call_log

    # ── parsing ──────────────────────────────────────────────────────
    @staticmethod
    def _parse_tool_call(text: str | None) -> dict[str, Any] | None:
        """Extract the first valid TOOL_CALL JSON from the LLM response.

        Scans each line independently so duplicate / multi-line output from
        the LLM doesn't break JSON parsing.
        """
        if not text:
            return None
        found_any = False
        for line in text.splitlines():
            m = _TOOL_CALL_LINE_RE.search(line)
            if not m:
                continue
            found_any = True
            raw = m.group(1)
            # Try parsing the captured group directly
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and "tool" in payload:
                    return payload
            except json.JSONDecodeError:
                pass
            # Fallback 1: fix unescaped Windows backslashes and retry
            fixed = MCPClient._fix_backslashes(raw)
            try:
                payload = json.loads(fixed)
                if isinstance(payload, dict) and "tool" in payload:
                    return payload
            except json.JSONDecodeError:
                pass
            # Fallback 2: find the balanced JSON object in the raw capture
            obj = MCPClient._extract_json_object(fixed)
            if obj and "tool" in obj:
                return obj
        if found_any:
            logger.warning("TOOL_CALL text found but JSON could not be parsed")
        return None

    @staticmethod
    def _extract_json_object(s: str) -> dict[str, Any] | None:
        """Find the first balanced { ... } in *s* and parse it."""
        start = s.find("{")
        if start == -1:
            return None
        depth = 0
        for i, ch in enumerate(s[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _fix_backslashes(raw: str) -> str:
        """Escape single backslashes inside JSON string values.

        LLMs often emit Windows paths like ``D:\\dover`` with only one
        backslash, which is invalid JSON.  This replaces lone ``\\`` with
        ``\\\\`` only inside quoted strings, leaving already-escaped ``\\\\``
        intact.
        """
        def _escape_in_strings(m: re.Match) -> str:
            content = m.group(0)
            return re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r'\\\\', content)

        return re.sub(r'"[^"]*"', _escape_in_strings, raw)

    @staticmethod
    def _strip_tool_call_lines(text: str | None) -> str:
        """Remove TOOL_CALL lines from text so the user sees a clean reply."""
        if not text:
            return ""
        cleaned = []
        for line in text.splitlines():
            if not _TOOL_CALL_LINE_RE.search(line):
                cleaned.append(line)
        return "\n".join(cleaned).strip()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def disconnect(self) -> None:
        for conn in self._servers:
            await conn.disconnect()
        await self.llm_api.close()
