"""The output contract every command keeps, for agents and scripts alike (`ssisbliss commands` shows it).

- stdout is one JSON document (only install-skill --print and sql-diff --markdown print text);
- `ok` is true exactly when the exit code is 0;
- when it is false, `error_kind` says why (the exit code in words, see EXIT_KINDS) and the document says what
  to do: `error`, or a plan's `refused_because`, or lint's findings;
- `next`, when there is one, lists what to do next: commands ready to run, with this command's --env or
  --server, or an action for the developer.
"""
from __future__ import annotations

import argparse
import difflib
import re
import shlex

SCHEMA_VERSION = 1

EXIT_KINDS = {
    0: ("ok", "done; for a deploy without --apply: a plan that can be applied"),
    1: ("error", "unexpected, or the server could not be reached; `error` has the details"),
    2: ("refused", "not done: a check, the input or the target said no, or lint found problems; the document "
                   "says why (`error`, `refused_because`, `findings`)"),
    3: ("plan_mismatch", "--plan-id is not the plan this command makes now: something changed; plan again"),
    4: ("verify_failed", "applied, but the server does not hold what was deployed; see `verify` and `rollback`"),
    5: ("usage", "unknown command or option, or a missing argument; `next` has the help to read"),
}
EXIT_USAGE = 5

# Commands whose --env/--server is not a deploy target (or that take none): `next` commands get no target.
_NO_TARGET = {"lint", "format", "sql-diff", "build", "install-skill", "guide", "commands", "mcp"}
# Top-level keys some commands already give a command in; `next` repeats them so there is one place to look.
_HINT_KEYS = ("deploy_your_changes", "remove_command", "format_command", "fix")


class UsageError(Exception):
    def __init__(self, prog: str, message: str):
        super().__init__(message)
        self.prog, self.message = prog, message


class Parser(argparse.ArgumentParser):
    """argparse that raises UsageError instead of printing text and exiting, so usage errors are JSON too."""

    def error(self, message: str):   # type: ignore[override]
        raise UsageError(self.prog, message)


def usage_doc(e: UsageError, argv: list[str], commands: list[str]) -> dict:
    wrong = re.search(r"invalid choice: '([^']*)'", e.message)
    if e.prog == "ssisbliss" and wrong and wrong.group(1) not in commands:
        close = difflib.get_close_matches(wrong.group(1), commands, n=3, cutoff=0.4)
        close += [c for c in commands if wrong.group(1) in c and c not in close]
        nxt = [f"ssisbliss {c} --help" for c in close[:3]] + ["ssisbliss commands"]
        return {"error": f"no command '{wrong.group(1)}'" + (f"; did you mean: {', '.join(close[:3])}" if close else ""),
                "next": nxt}
    if e.prog == "ssisbliss" and "required: cmd" in e.message:
        return {"error": "give a command", "next": ["ssisbliss guide", "ssisbliss commands"]}
    cmd = e.prog if e.prog != "ssisbliss" else f"ssisbliss {argv[0]}" if argv and argv[0] in commands else "ssisbliss"
    return {"error": e.message, "next": [f"{cmd} --help"]}


def _nested_next(doc, found: list[str], top: bool = True) -> None:
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "next" and not top and isinstance(v, str):
                found.append(v)
            elif isinstance(v, (dict, list)):
                _nested_next(v, found, False)
    elif isinstance(doc, list):
        for v in doc:
            _nested_next(v, found, False)


def _with_target(cmd: str, a) -> str:
    """`ssisbliss history "JOB"` -> `ssisbliss history --env dev "JOB"`: ready to run against the same server."""
    parts = cmd.split(" ", 2)
    if len(parts) < 2 or parts[0] != "ssisbliss" or parts[1] in _NO_TARGET or " --env " in f"{cmd} " \
            or " --server " in f"{cmd} ":
        return cmd
    env, server = getattr(a, "env", None), getattr(a, "server", None)
    target = f"--env {shlex.quote(env)}" if env else f"--server {shlex.quote(server)}" if server else ""
    return " ".join([parts[0], parts[1], target, *parts[2:]]) if target else cmd


def envelope(doc: dict, code: int, a=None, target_from: dict | None = None) -> dict:
    """The document as it is printed: ok, error_kind, the reason, target_from and next first, then the rest."""
    own = doc.pop("next", None)
    nxt = [own] if isinstance(own, str) else list(own or [])
    nxt += [v for k in _HINT_KEYS if isinstance(v := doc.get(k), str) and v.startswith("ssisbliss ")]
    nested: list[str] = []
    _nested_next(doc, nested)
    nxt += nested
    seen: list[str] = []
    for n in (_with_target(n, a) for n in nxt):
        if n not in seen:
            seen.append(n)
    head: dict = {"ok": code == 0}
    if code:
        head["error_kind"] = EXIT_KINDS.get(code, ("error", ""))[0]
    head.update({k: doc.pop(k) for k in ("error", "refused_because") if k in doc})
    if target_from:
        head["target_from"] = dict(target_from)
    if seen:
        head["next"] = seen[:5]
    return {**head, **doc}
