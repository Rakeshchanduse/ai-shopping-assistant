"""
Terminal chat interface for the MCP Client.
Run with: python chat_interface.py
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import (
    CHAT_HISTORY_FILE,
    GAINS_API_TOKEN,
    GAINS_API_URL,
    GEMINI_MODEL,
    GOOGLE_API_KEY,
    LLM,
    MCP_SERVER_SCRIPT,
)
from mcp_client import MCPClient

console = Console()
logger = logging.getLogger(__name__)


def _print_tools(tools: list[dict[str, Any]]) -> None:
    table = Table(title="Available MCP Tools", show_lines=True)
    table.add_column("Tool", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")
    for t in tools:
        table.add_row(t["name"], t["description"])
    console.print(table)


def _print_servers(client: Any) -> None:
    table = Table(title="MCP Servers", show_lines=True)
    table.add_column("#", style="bold", no_wrap=True)
    table.add_column("Server", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Tools", style="dim")
    for i, name in enumerate(client.get_server_names(), 1):
        active = name in client.active_servers
        status = "[green]ON[/green]" if active else "[red]OFF[/red]"
        tool_names = ", ".join(t["name"] for t in client._server_tools.get(name, []))
        table.add_row(str(i), name, status, tool_names)
    console.print(table)


def _print_tool_calls(log: list[dict[str, Any]]) -> None:
    if not log:
        return
    for entry in log:
        console.print(Text(f"  -> tool: {entry['tool']}  args: {entry['args']}", style="dim"))
        result_preview = entry["result"][:300]
        if len(entry["result"]) > 300:
            result_preview += " ..."
        console.print(Text(f"    result: {result_preview}", style="dim green"))


def _create_llm_api():
    """Factory: return the LLM api object selected by LLM env var."""
    if LLM == "gemini":
        if not GOOGLE_API_KEY:
            console.print("[bold red]Error:[/bold red] GOOGLE_API_KEY is not set. Set it in .env")
            return None
        from gemini_api import GeminiAPI
        return GeminiAPI(api_key=GOOGLE_API_KEY, model=GEMINI_MODEL)
    else:  # default: gains
        if not GAINS_API_TOKEN:
            console.print("[bold red]Error:[/bold red] GAINS_API_TOKEN is not set. Set it in .env")
            return None
        from gains_api import GainsAPI
        return GainsAPI(base_url=GAINS_API_URL, token=GAINS_API_TOKEN)


async def main() -> None:
    llm_label = "Google Gemini" if LLM == "gemini" else "Gains AI"
    console.print(Panel(
        f"[bold cyan]E-Commerce MCP Chat Interface[/bold cyan]\n"
        f"Powered by {llm_label} + MCP e-commerce tools.\n"
        "Type [bold]/help[/bold] for commands. Type [bold]exit[/bold] to quit.",
        expand=False,
    ))

    api = _create_llm_api()
    if api is None:
        return

    client = MCPClient(api)

    console.print("[dim]Connecting to MCP server ...[/dim]")
    try:
        await client.connect(MCP_SERVER_SCRIPT)
    except Exception as exc:
        console.print(f"[bold red]Failed to connect:[/bold red] {exc}")
        return

    _print_servers(client)
    _print_tools(client.get_active_tools())
    console.print()

    session: PromptSession[str] = PromptSession(history=FileHistory(CHAT_HISTORY_FILE))

    try:
        while True:
            try:
                active = "+".join(sorted(client.active_servers))
                prompt_str = f"\nShoppingBot [{active}] > "
                user_input: str = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: session.prompt(prompt_str)
                )
            except (EOFError, KeyboardInterrupt):
                break

            stripped = user_input.strip().lower()
            if stripped in ("exit", "quit", "/exit", "/quit"):
                break
            if not stripped:
                continue

            if stripped == "/tools":
                _print_tools(client.get_active_tools())
                continue
            if stripped == "/servers":
                _print_servers(client)
                continue
            if stripped == "/clear":
                client.session_id = None
                console.print("[yellow]Session cleared.[/yellow]")
                continue
            if stripped == "/help":
                console.print(
                    "[bold]Commands:[/bold]\n"
                    "  /tools   - list active tools\n"
                    "  /servers - show MCP servers\n"
                    "  /clear   - reset session\n"
                    "  /help    - show this help\n"
                    "  exit     - quit"
                )
                continue

            try:
                status_ctx = console.status("[bold green]Sending query ...[/bold green]")
                status_ctx.start()

                def _on_status(msg: str) -> None:
                    status_ctx.update(f"[bold green]{msg}[/bold green]")

                reply, tool_log = await client.chat(user_input, on_status=_on_status)
                status_ctx.stop()
                _print_tool_calls(tool_log)
                console.print(Panel(
                    Markdown(reply) if reply else Text("(no response)", style="dim"),
                    title="Assistant", border_style="cyan",
                ))
            except Exception as exc:
                status_ctx.stop()
                console.print(f"[bold red]Error:[/bold red] {exc}")
                logger.exception("Chat error")
    finally:
        console.print("[yellow]Disconnecting ...[/yellow]")
        await client.disconnect()
        console.print("[green]Goodbye![/green]")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
