"""Loads skill-pack markdown files as system-prompt context.

Skill docs are the only place project-specific knowledge lives (never
inlined as Python strings) — see the "Extension seams" section of the
build plan. This is what makes Dimension 4's future self-improvement loop
(the agent proposing skill-file diffs from override patterns) mechanically
simple: it's a PR against a markdown file, same as a human would write.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "kyverno"


def load_skill(name: str) -> str:
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"No skill doc at {path}")
    return path.read_text()
