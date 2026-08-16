"""GitHub authentication, behind one interface with two implementations.

Production path: `GitHubAppAuth` — a GitHub App with installation-scoped,
auto-rotating tokens (§5.2: "the App only has access to the repos it's
installed on... branch protection rules apply to App tokens too, so it
cannot push to main even if it tried"). This is what ships in the demo.

Local-dev path: `GitHubPATAuth` — a fine-grained personal access token, so
agent logic (merge_policy.py, the agents themselves) can be built and unit
tested in hour one, before the GitHub App is registered. It is never used
once the App is wired up and never appears in the recorded demo.

Both implement `GitHubAuth.get_client(repo_full_name) -> Github`, so
nothing downstream (github_tools.py, the agents) needs to know or care
which one is active.
"""

from __future__ import annotations

import os
from typing import Protocol

from github import Auth, Github, GithubIntegration
from loguru import logger


class GitHubAuth(Protocol):
    def get_client(self, repo_full_name: str) -> Github: ...
    def get_token(self, repo_full_name: str) -> str: ...


class GitHubPATAuth:
    """Local-dev-only. A single token, same client for every repo."""

    def __init__(self, token: str) -> None:
        self._token = token

    def get_client(self, repo_full_name: str) -> Github:
        return Github(auth=Auth.Token(self._token))

    def get_token(self, repo_full_name: str) -> str:
        return self._token


class GitHubAppAuth:
    """Production path. Exchanges the App's JWT for a short-lived,
    installation-scoped token, per repo, per call — matches §5.2 exactly:
    least-privilege, installation-scoped, auto-rotating."""

    def __init__(self, app_id: str, private_key: str) -> None:
        self._integration = GithubIntegration(auth=Auth.AppAuth(app_id, private_key))

    def get_client(self, repo_full_name: str) -> Github:
        owner, repo = repo_full_name.split("/", 1)
        installation = self._integration.get_repo_installation(owner, repo)
        return self._integration.get_github_for_installation(installation.id)

    def get_token(self, repo_full_name: str) -> str:
        """Raw bearer token for the rare caller that needs one directly
        instead of a `Github` client — currently just
        `discussion_tools.add_discussion_comment`, since GitHub Discussions
        has no REST API and PyGithub has no GraphQL helper to hand a token
        to internally."""
        owner, repo = repo_full_name.split("/", 1)
        installation = self._integration.get_repo_installation(owner, repo)
        return self._integration.get_access_token(installation.id).token


def get_auth_from_env() -> GitHubAuth:
    """Prefers the GitHub App if its env vars are set; falls back to a PAT
    for local dev. Raises if neither is configured — fail loud, not silent."""
    app_id = os.environ.get("GITHUB_APP_ID")
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if app_id and key_path and os.path.exists(key_path):
        logger.info("Using GitHub App auth (installation-scoped, auto-rotating)")
        with open(key_path) as f:
            private_key = f.read()
        return GitHubAppAuth(app_id, private_key)

    pat = os.environ.get("GITHUB_PAT")
    if pat:
        logger.warning("Using local-dev PAT auth — never used in the recorded demo")
        return GitHubPATAuth(pat)

    raise RuntimeError(
        "No GitHub auth configured. Set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH "
        "(production) or GITHUB_PAT (local dev only) in the environment."
    )
