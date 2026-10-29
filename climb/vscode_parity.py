#!/usr/bin/env python3
"""Do the climb's agents match what VS Code users run?

Claude Code and Codex in VS Code are extensions that run the same agents as the command-line tools
the climb uses (the Claude Code extension launches Claude Code; the Codex extension ships the codex
binary), and they read skills from the same folders (~/.claude/skills, ~/.codex/skills). This script
asks the VS Code Marketplace which versions the extensions ship now and compares them with the local
CLIs. It only reads version numbers: nothing is downloaded or run.

    uv run python climb/vscode_parity.py
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import urllib.request

EXTENSIONS = {"claude": "anthropic.claude-code", "codex": "openai.chatgpt"}


def target_platform() -> str:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system(), "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def marketplace_version(extension: str, plat: str) -> str | None:
    body = json.dumps({"filters": [{"criteria": [{"filterType": 7, "value": extension}]}], "flags": 0x192}).encode()
    req = urllib.request.Request("https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery", data=body,
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json;api-version=7.2-preview.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        ext = json.load(r)["results"][0]["extensions"][0]
    for v in ext["versions"]:
        if v.get("targetPlatform") in (plat, None):
            return v["version"]
    return None


def cli_version(cmd: str) -> str | None:
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else None


def compare(extension: str | None, cli: str | None) -> str:
    if not extension or not cli:
        return "unknown"
    e, c = (tuple(map(int, x.split(".")[:3])) for x in (extension, cli))
    if e == c:
        return "same version"
    if e[:2] == c[:2]:
        return f"same release line ({'extension newer' if e > c else 'CLI newer'} by {abs(e[2] - c[2])} patch versions)"
    return "extension newer: update the CLI (`claude update`) before trusting the climb for VS Code" if e > c else "CLI newer"


def main() -> int:
    plat = target_platform()
    claude_ext = marketplace_version(EXTENSIONS["claude"], plat)
    codex_ext = marketplace_version(EXTENSIONS["codex"], plat)
    doc = {
        "platform": plat,
        "claude_code": {"vscode_extension": claude_ext, "cli": cli_version("claude"),
                        "parity": compare(claude_ext, cli_version("claude")),
                        "skills": "~/.claude/skills and .claude/skills, same as the CLI"},
        "codex": {"vscode_extension": codex_ext, "cli": cli_version("codex"),
                  "parity": "the extension ships its own codex binary, numbered differently from the extension; "
                            "the agent and its skill folders (~/.codex/skills, .agents/skills) are the CLI's",
                  "skills": "~/.codex/skills and .agents/skills"},
        "github_copilot": {"parity": "a different agent (built into VS Code); needs a GitHub sign-in to test",
                           "skills": "~/.copilot/skills, .github/skills (ssisbliss install-skill --copilot)"},
    }
    print(json.dumps(doc, indent=1))
    return 0 if "update the CLI" not in doc["claude_code"]["parity"] else 1


if __name__ == "__main__":
    sys.exit(main())
