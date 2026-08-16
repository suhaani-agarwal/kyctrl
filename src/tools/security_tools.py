"""Security Agent's tool server. Deliberately the smallest tool server in
this codebase: exactly one tool, and it is NOT `comment_on_issue` — per
kyctrl_extra_features.md Dimension 2, this agent has "no access to public
comment posting" at all. `file_private_report` is the only capability
offered; there is no code path from this server to a public GitHub comment,
so the model cannot talk its way into one no matter what it's asked to do —
the same "capability doesn't exist" pattern as `allow_merge` in
`github_tools.py`, applied to an entire tool category instead of one tool.
"""

from __future__ import annotations

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from loguru import logger

from src.tools.slack_tools import post_message


def build_security_report_tool_server(issue_number: int, repo_full_name: str, private_slack_channel: str) -> McpSdkServerConfig:
    @tool(
        "file_private_report",
        "File a private security assessment. This is NEVER posted to the public issue thread — "
        "it goes only to the audit log and (if configured) a private maintainer Slack channel.",
        {"summary": str, "severity": str, "affected_component": str},
    )
    async def file_private_report(args: dict) -> dict:
        body = (
            f"Private security report — issue #{issue_number} ({repo_full_name})\n"
            f"Severity: {args['severity']}\n"
            f"Affected component: {args['affected_component']}\n\n"
            f"{args['summary']}"
        )
        posted = post_message(private_slack_channel, body)
        logger.info(f"Security Agent filed private report for issue #{issue_number} (slack_posted={posted})")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"private report filed (slack_posted={posted}); never posted publicly",
                }
            ]
        }

    return create_sdk_mcp_server(name="security-tools", tools=[file_private_report])
