"""Small helpers shared by more than one agent module. Deliberately not a
"utils" grab-bag — only things that would otherwise be copy-pasted between
agents live here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.config import AiMaintainerConfig
from src.memory import search_context, write_episode
from src.runtime import get_config, get_memory_client


def is_bot_author(login: str, config: AiMaintainerConfig) -> bool:
    """True if `login` is one of the configured dependency-bot accounts
    (`dependabot.bot_usernames`). Used by `dependabot.py` to decide whether
    a PR is its concern, and by `coach.py` to decide the opposite — a human
    contributor's PR is Coach's concern, a bot's is not. Single-sourced here
    so the two agents can never disagree about who counts as a bot."""
    return login in config.dependabot.bot_usernames


# --- Dimension 3 (Graphiti temporal memory) — the one integration point
# every `handle_*`/`answer_question` entrypoint uses, so the "is memory
# enabled, did this call fail" guard exists exactly once instead of seven
# times (dependabot, issue_triage, coach, security_agent, pattern_agent,
# qa_assistant, reproduction). See src/memory.py's module docstring for the
# why; these two functions are just the "no-op when memory's off or
# unreachable" wrapper around it — same shape `get_repo_variable` gives
# the kill switch's live half.


async def memory_search(query: str, limit: int | None = None) -> list[str]:
    """Facts relevant to `query`, or `[]` if memory is disabled/unavailable
    this run. Called before building a prompt, to prefetch deterministic
    context the same way `issue_triage.py` prefetches its deterministic
    classification line — never a reason to change *whether* an agent runs,
    only what it's told going in."""
    memory = get_memory_client()
    if memory is None:
        return []
    return await search_context(memory, query=query, limit=limit or get_config().memory.search_top_k)


async def memory_write(name: str, episode_body: str, source_description: str) -> list[str] | None:
    """Records one run's outcome as a Graphiti episode, returning the
    node/edge UUIDs for `AuditEntry.memory_refs` — or `None` if memory is
    disabled/unavailable this run, which every caller passes straight
    through to `memory_refs` unchanged (`None` there already means "nothing
    to reference", so this needs no special-casing at the call site)."""
    memory = get_memory_client()
    if memory is None:
        return None
    return await write_episode(
        memory,
        name=name,
        episode_body=episode_body,
        source_description=source_description,
        reference_time=datetime.now(timezone.utc),
    )
