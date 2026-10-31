"""ssisbliss about itself, for an agent that has no skill installed: `commands` (every command and option, as
JSON) and `guide` (the skill's rules and recipes)."""
from __future__ import annotations

import argparse

from ..contract import EXIT_KINDS, SCHEMA_VERSION
from .common import Reply, reply
from .skill import skill_text

# Commands that write files on this machine (an applied deploy also writes .ssis-base.json records).
WRITES_FILES = {"pull", "format", "build", "install-skill"}
GUIDE_TOPICS = {"rules": "## Rules", "deploy": "## Deploying", "review": "## Reviewing", "troubleshoot": "## Why did"}


def _subcommands(p: argparse.ArgumentParser) -> dict[str, tuple[argparse.ArgumentParser, str]]:
    sub = next(x for x in p._actions if isinstance(x, argparse._SubParsersAction))
    helps = {c.dest: c.help or "" for c in sub._choices_actions}
    return {name: (sp, helps.get(name, "")) for name, sp in sub.choices.items()}


def arguments(sp: argparse.ArgumentParser) -> list[dict]:
    """A command's options and positional arguments: name, flag, type, whether required, choices, default, help."""
    out = []
    for x in sp._actions:
        if isinstance(x, argparse._HelpAction) or x.help == argparse.SUPPRESS:
            continue
        positional = not x.option_strings
        many = x.nargs in ("*", "+") or isinstance(x, argparse._AppendAction)
        kind = ("boolean" if isinstance(x, argparse._StoreTrueAction) else
                "integer" if x.type is int else "number" if x.type is float else "string")
        arg: dict = {"name": x.dest, "flag": None if positional else max(x.option_strings, key=len),
                     "type": "array" if many else kind,
                     "required": bool(x.required) if not positional else x.nargs not in ("?", "*")}
        if many:
            arg["items"] = kind
        if x.choices:
            arg["choices"] = list(x.choices)
        if x.default not in (None, False, [], argparse.SUPPRESS):
            arg["default"] = x.default
        if x.help:
            arg["help"] = x.help
        out.append(arg)
    return out


def catalog(p: argparse.ArgumentParser) -> dict[str, dict]:
    """Every command: what it does, whether it changes a server or writes files, and its arguments."""
    out = {}
    for name, (sp, summary) in _subcommands(p).items():
        args = arguments(sp)
        out[name] = {"summary": summary, "changes_server": any(a["flag"] == "--apply" for a in args),
                     "writes_files": name in WRITES_FILES, "arguments": args}
    return out


def cmd_commands(a) -> Reply:
    from ..cli import parser
    found = catalog(parser())
    if a.name:
        if a.name not in found:
            raise ValueError(f"no command '{a.name}'; the commands are {', '.join(found)}")
        return reply({"command": a.name, **found[a.name], "usage": _subcommands(parser())[a.name][0].format_usage().strip()})
    return reply({
        "schema_version": SCHEMA_VERSION,
        "contract": ("stdout is one JSON document; `ok` is true exactly when the exit code is 0; when it is false, "
                     "`error_kind` says why and `error`, `refused_because` or `findings` what; `next` lists what to "
                     "do next (commands ready to run). Nothing changes on a server without --apply and the plan_id "
                     "of a plan you just made: run a deploy once without --apply, read the plan, then its `next`."),
        "exit_codes": {str(code): {"error_kind": kind, "meaning": meaning} for code, (kind, meaning) in EXIT_KINDS.items()},
        "commands": {n: {k: c[k] for k in ("summary", "changes_server", "writes_files")} for n, c in found.items()},
        "next": ["ssisbliss guide", "ssisbliss commands NAME"],
    })


def cmd_guide(a) -> Reply:
    text = skill_text().split("\n---\n", 1)[-1].strip()   # without the skill's front matter
    if a.topic != "all":
        start = text.index(GUIDE_TOPICS[a.topic])
        end = text.find("\n## ", start + 1)
        text = text[start:end if end > 0 else None].strip()
    return reply({"topic": a.topic, "guide": text, "topics": ["all", *GUIDE_TOPICS]})
