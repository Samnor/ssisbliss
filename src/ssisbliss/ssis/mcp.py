"""`ssisbliss mcp`: every command as an MCP tool, over stdio (JSON-RPC, one message per line).

For agents that call tools rather than run a shell. A tool runs exactly what the command of the same name
runs (cli.run), with the same output contract and the same safety: a deploy tool changes nothing without
apply=true and the plan_id of the plan it just returned. Tool schemas come from the command's options, so
they cannot drift from the CLI. No MCP library is needed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

from .. import __version__

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
NOT_TOOLS = {"mcp", "install-skill"}
_JSON = {"boolean": "boolean", "integer": "integer", "number": "number", "string": "string"}

INSTRUCTIONS = """\
ssisbliss: SSIS packages, SSIS catalogs (SSISDB) and SQL Agent jobs. Every command of the ssisbliss CLI is a
tool here, named with underscores (deploy-packages -> deploy_packages); its options are the tool's arguments
without the dashes (--plan-id -> plan_id). Commands in the guide and in `next` are written as CLI commands:
call the tool of that name with those arguments. A tool's result is the command's JSON document: `ok`,
`error_kind`, `next`. Nothing changes on a server without apply=true and the plan_id of a plan the same tool
just returned. Call `guide` once for the rules and recipes.
"""


def instructions() -> str:
    """How to use the tools, and the skill's rules: an agent over MCP has no skill and may never call guide."""
    from .commands.skill import skill_text
    text = skill_text()
    start = text.index("## Rules")
    return INSTRUCTIONS + "\n" + text[start:text.index("\n## ", start + 1)].strip() + "\n"


def _tool_name(command: str) -> str:
    return command.replace("-", "_")


def _schema(sp: argparse.ArgumentParser) -> dict:
    from .commands.about import arguments
    props: dict = {}
    required = []
    for a in arguments(sp):
        p: dict = {"type": a["type"]} if a["type"] != "array" else {"type": "array", "items": {"type": _JSON[a["items"]]}}
        if "choices" in a:
            (p["items"] if a["type"] == "array" else p)["enum"] = a["choices"]
        if "default" in a:
            p["default"] = a["default"]
        if "help" in a:
            p["description"] = a["help"]
        props[a["name"]] = p
        if a["required"]:
            required.append(a["name"])
    props["cwd"] = {"type": "string", "description": "directory to run in, for relative paths and the .dtproj "
                                                     "(default: the server's working directory)"}
    return {"type": "object", "properties": props, **({"required": required} if required else {})}


def tools() -> list[dict]:
    from .cli import parser
    from .commands.about import WRITES_FILES, _subcommands, catalog
    found = catalog(parser())
    out = []
    for name, (sp, summary) in _subcommands(parser()).items():
        if name in NOT_TOOLS:
            continue
        changes = found[name]["changes_server"]
        text = summary + (f". {sp.description}" if sp.description and sp.description not in summary else "")
        if changes:
            text += (". Changes the server only with apply=true and plan_id: call it first without apply, read the "
                     "plan's checks, then call it again with apply=true and the plan_id it returned. A refused plan is the "
                     "answer: tell the developer; never commit, push or use overwrite to get past it")
        out.append({"name": _tool_name(name), "description": text, "inputSchema": _schema(sp),
                    "annotations": {"title": name, "readOnlyHint": not changes and name not in WRITES_FILES,
                                    "destructiveHint": changes, "openWorldHint": False}})
    return out


def argv_for(command: str, args: dict) -> list[str]:
    """The command line a tool call stands for: positionals in order, then options."""
    from .cli import parser
    from .commands.about import _subcommands
    sp = _subcommands(parser())[command][0]
    actions = {x.dest: x for x in sp._actions if not isinstance(x, argparse._HelpAction)}
    unknown = sorted(set(args) - set(actions) - {"cwd"})
    if unknown:
        raise ValueError(f"{_tool_name(command)} has no argument {', '.join(unknown)}; it has {', '.join(sorted(actions))}")
    positional, options = [], []
    for dest, x in actions.items():
        v = args.get(dest)
        if v is None or v is False or v == []:
            continue
        values = [str(i) for i in v] if isinstance(v, list) else [str(v)]
        if not x.option_strings:
            positional += values
        elif isinstance(x, argparse._StoreTrueAction):
            options.append(max(x.option_strings, key=len))
        elif isinstance(x, argparse._AppendAction):
            for i in values:
                options += [max(x.option_strings, key=len), i]
        else:
            options += [max(x.option_strings, key=len), *values]
    return [command, *options, *(["--", *positional] if any(p.startswith("-") for p in positional) else positional)]


def call(name: str, args: dict) -> tuple[str, dict | None, bool]:
    """Run a tool: (text, the JSON document or None for text output, whether it is an error)."""
    from .cli import run
    command = next((t["annotations"]["title"] for t in tools() if t["name"] == name), None)
    if command is None:
        doc = {"ok": False, "error_kind": "usage", "error": f"no tool '{name}'", "next": ["commands"]}
        return json.dumps(doc), doc, True
    try:
        argv = argv_for(command, args)
    except ValueError as e:
        doc = {"ok": False, "error_kind": "usage", "error": str(e)}
        return json.dumps(doc), doc, True
    here = os.getcwd()
    try:
        if args.get("cwd"):
            os.chdir(args["cwd"])
        with contextlib.redirect_stdout(sys.stderr):   # stdout carries the protocol only
            r = run(argv)
    finally:
        os.chdir(here)
    if r.text is not None:
        return r.text, None, r.code != 0
    if r.doc.get("next"):   # help is a shell's; here a command's arguments come from `commands`
        r.doc["next"] = [f"ssisbliss commands {n.split()[1]}" if n.startswith("ssisbliss ") and n.endswith(" --help")
                         else n for n in r.doc["next"]]
    return json.dumps(r.doc, indent=1, default=str), json.loads(json.dumps(r.doc, default=str)), r.code != 0


def handle(msg: dict, state: dict) -> dict | None:
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None   # a notification (initialized, cancelled): nothing to answer
    if method == "initialize":
        wanted = params.get("protocolVersion")
        state["protocol"] = wanted if wanted in PROTOCOLS else PROTOCOLS[0]
        result: dict = {"protocolVersion": state["protocol"], "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "ssisbliss", "version": __version__}, "instructions": instructions()}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools()}
    elif method == "tools/call":
        text, doc, error = call(params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": error}
        if doc is not None and state.get("protocol", PROTOCOLS[0]) >= "2025-06-18":
            result["structuredContent"] = doc
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    state: dict = {}
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            answer: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}
        else:
            try:
                answer = handle(msg, state)
            except Exception as e:  # noqa: BLE001 - a broken call must not end the session
                answer = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if answer is not None:
            stdout.write(json.dumps(answer) + "\n")
            stdout.flush()
    return 0
