"""Small helpers shared by more than one agent module. Deliberately not a
"utils" grab-bag — only things that would otherwise be copy-pasted between
agents live here.
"""

from __future__ import annotations

from src.config import AiMaintainerConfig


def is_bot_author(login: str, config: AiMaintainerConfig) -> bool:
    """True if `login` is one of the configured dependency-bot accounts
    (`dependabot.bot_usernames`). Used by `dependabot.py` to decide whether
    a PR is its concern, and by `coach.py` to decide the opposite — a human
    contributor's PR is Coach's concern, a bot's is not. Single-sourced here
    so the two agents can never disagree about who counts as a bot."""
    return login in config.dependabot.bot_usernames
