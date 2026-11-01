"""The ssisbliss skill: the rules and recipes agents follow, installed for them or printed."""
from __future__ import annotations

from pathlib import Path

from .common import Reply, reply


def skill_text() -> str:
    from importlib.resources import files
    return (files("ssisbliss") / "skills" / "ssisbliss" / "SKILL.md").read_text(encoding="utf-8")


def cmd_install_skill(a) -> Reply:
    """Install the ssisbliss skill at user level (nothing goes into the repository) for Claude Code, Codex and
    GitHub Copilot, which includes Copilot in VS Code (~/.copilot/skills; VS Code's local sessions also read
    ~/.claude/skills)."""
    if a.print:
        return Reply(None, text=skill_text())
    home = Path(a.home).expanduser() if a.home else Path.home()
    targets = {"claude": home / ".claude" / "skills", "codex": home / ".codex" / "skills",
               "copilot": home / ".copilot" / "skills"}
    chosen = [t for t in targets if getattr(a, t)] or [t for t, d in targets.items() if d.parent.exists()]
    if not chosen:
        raise ValueError("none of ~/.claude, ~/.codex or ~/.copilot exists; pass --claude, --codex or --copilot to create one")
    written = []
    for t in chosen:
        dest = targets[t] / "ssisbliss" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(skill_text(), encoding="utf-8")
        written.append(str(dest))
    return reply({"installed": written, "note": "agents pick it up in new sessions (in VS Code: a new chat)"})

