"""Rich-powered terminal streaming for agent runs.

The video needs "show the agent reasoning in a terminal stream" (§6 of
kyctrl_plan.md) to look like a product, not a log dump. This renders a
live-updating panel: the agent's text reasoning grows as it streams, and
each tool call it makes appears as a line below, in real time.
"""

from __future__ import annotations

from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


async def stream_agent_run(
    messages: AsyncIterator, *, title: str, console=None
) -> ResultMessage | None:
    """Drain a `query()` stream, rendering it live, and return the final
    `ResultMessage` (or None if the stream never produced one)."""
    reasoning = Text()
    tool_calls: list[str] = []
    result: ResultMessage | None = None

    def render() -> Panel:
        body = Group(
            reasoning,
            Text("\n".join(f"  → {c}" for c in tool_calls), style="cyan") if tool_calls else Text(""),
        )
        return Panel(body, title=title, border_style="green")

    with Live(render(), console=console, refresh_per_second=12) as live:
        async for message in messages:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reasoning.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(f"{block.name}({block.input})")
                    elif isinstance(block, ToolResultBlock) and block.is_error:
                        tool_calls.append(f"[error] {block.content}")
            elif isinstance(message, ResultMessage):
                result = message
            live.update(render())

    return result
