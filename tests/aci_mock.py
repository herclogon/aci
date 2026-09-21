"""In-process mock ACI server for LLM evaluation.

Implements the parts of SPEC.md an agent actually touches: line lexing,
path resolution, flag rules, @input, type coercion, help/--help, search,
explore/scenario, tasks, --dry-run/--yes, and the error catalogue with
did_you_mean. Domain behaviour comes from tests/mock_app.json plus a few
hand-written command handlers below. Nothing here talks to a network.
"""
from __future__ import annotations

import copy
import difflib
import json
import re
from pathlib import Path
from typing import Any

ACI_VERSION = "0.2"
GLOBAL_FLAGS = {"help", "format", "fields", "limit", "cursor", "dry-run", "yes", "async", "quiet"}
BOOL_GLOBALS = {"help", "dry-run", "yes", "async", "quiet"}
SHELL_CHARS = set("|&;<>()$`*?[~")
DEFAULT_LIMIT = 20


class AciError(Exception):
    def __init__(self, code: str, message: str, hint: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.hint, self.extra = code, message, hint, extra


# ---------------------------------------------------------------- lexing

def lex_line(line: str, app_name: str) -> list[str]:
    """§4.3.1: POSIX-ish word splitting, reject *unquoted* shell operators."""
    # Tolerance (§4.3.1): a leading prompt glyph is stripped before the operator check.
    line = re.sub(r"^\s*[$>]\s+", "", line)
    tokens: list[str] = []
    cur: list[str] = []
    in_tok = False
    quote: str | None = None
    i = 0
    unquoted_ops: list[str] = []
    while i < len(line):
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                cur.append(c)
        elif quote == '"':
            if c == "\\" and i + 1 < len(line) and line[i + 1] in '"\\':
                cur.append(line[i + 1]); i += 1
            elif c == '"':
                quote = None
            else:
                cur.append(c)
        elif c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1]); in_tok = True; i += 1
        elif c in "'\"":
            quote = c; in_tok = True
        elif c.isspace():
            if in_tok:
                tokens.append("".join(cur)); cur = []; in_tok = False
        else:
            if c in SHELL_CHARS:
                unquoted_ops.append(c)
            cur.append(c); in_tok = True
        i += 1
    if quote:
        raise AciError("invalid_args", "Unterminated quote in line.", "Close the quote, or pass the value via @input.")
    if in_tok:
        tokens.append("".join(cur))
    if unquoted_ops:
        raise AciError(
            "shell_syntax_rejected",
            f"Unquoted shell character {unquoted_ops[0]!r} is not allowed; this is not a shell.",
            "Quote the value (\"...\") or pass it via `input` and reference it as @input.<key>. "
            "If you copied a step with <name>, replace the placeholder with a real value.",
        )
    # Tolerance (§4.3.1): strip an echoed app name.
    if tokens and tokens[0] == app_name:
        tokens = tokens[1:]
    return tokens


# ---------------------------------------------------------------- server

class MockServer:
    def __init__(self, fixture: str | Path | None = None):
        path = Path(fixture) if fixture else Path(__file__).with_name("mock_app.json")
        self.app = json.loads(Path(path).read_text())
        self.reset()

    # ---- state
    def reset(self) -> None:
        st = copy.deepcopy(self.app["state"])
        self.simulations: dict = st["simulations"]
        self.workflows: dict = st["workflows"]
        self.optimizations: dict = {}
        self.runs: dict = {}
        self.tasks: dict = {}
        self.counter = 10
        self.log: list[dict] = []  # every invoke, for scoring

    def _next(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter}"

    def handle_type(self, hid: str) -> str | None:
        if hid in self.simulations: return "simulation"
        if hid in self.workflows: return "workflow"
        if hid in self.optimizations: return "optimization"
        if hid in self.runs: return "run"
        if hid in self.tasks: return "task"
        return None

    # ---- tree helpers
    def manifest(self) -> dict:
        a = self.app
        return {
            "name": a["name"], "title": a["title"], "version": a["version"], "aci": ACI_VERSION,
            "levels": ["core", "search", "scenarios", "tasks"],
            "description": a["description"], "top_scenarios": a["top_scenarios"],
            "limits": {"max_help_depth": 2, "max_limit": 200, "default_limit": DEFAULT_LIMIT},
        }

    def root_children(self) -> list[dict]:
        ch = [{"name": g, "kind": "group", "summary": v["summary"]} for g, v in self.app["groups"].items()]
        ch += [
            {"name": "task", "kind": "group", "summary": "Track long-running operations"},
            {"name": "scenario", "kind": "group", "summary": "List and show step-by-step scenarios"},
            {"name": "info", "kind": "command", "summary": "Show the application manifest"},
            {"name": "help", "kind": "command", "summary": "List commands one level at a time"},
            {"name": "search", "kind": "command", "summary": "Find commands and scenarios by intent"},
            {"name": "explore", "kind": "command", "summary": "Get a step-by-step scenario for a goal"},
        ]
        return ch

    RESERVED = {
        "task": {"status": "Current state of a task", "wait": "Block until a task finishes", "cancel": "Request cancellation",
                 "logs": "Incremental log lines", "list": "Tasks visible to you"},
        "scenario": {"list": "List scenarios", "show": "Show one scenario with its steps"},
    }

    def descriptor(self, path: list[str]) -> dict | None:
        if len(path) == 2 and path[0] in self.app["groups"]:
            cmd = self.app["groups"][path[0]]["commands"].get(path[1])
            if cmd:
                d = dict(cmd); d["path"] = path
                d.setdefault("args", []); d.setdefault("flags", []); d.setdefault("effects", [])
                return d
        if len(path) == 2 and path[0] in self.RESERVED and path[1] in self.RESERVED[path[0]]:
            return self._reserved_descriptor(path)
        if len(path) == 1 and path[0] in ("info", "help", "search", "explore"):
            return self._reserved_descriptor(path)
        return None

    def _reserved_descriptor(self, path: list[str]) -> dict:
        p = ".".join(path)
        base = {"path": path, "flags": [], "args": [], "effects": [], "examples": [{"argv": path}]}
        table = {
            "info": ("Show the application manifest", [], []),
            "help": ("List commands one level at a time", [{"name": "path", "type": "string", "repeatable": True}], [{"name": "depth", "type": "int"}]),
            "search": ("Find commands and scenarios by intent", [{"name": "query", "type": "string", "required": True, "repeatable": True}],
                       [{"name": "kind", "type": "enum", "values": ["command", "scenario", "all"], "default": "all"}]),
            "explore": ("Get a step-by-step scenario for a goal", [{"name": "goal", "type": "string", "required": True, "repeatable": True}], []),
            "task.status": ("Current state of a task", [{"name": "id", "type": "handle:task", "required": True}], []),
            "task.wait": ("Block until a task finishes", [{"name": "id", "type": "handle:task", "required": True}], [{"name": "timeout", "type": "int"}]),
            "task.cancel": ("Request cancellation", [{"name": "id", "type": "handle:task", "required": True}], []),
            "task.logs": ("Incremental log lines", [{"name": "id", "type": "handle:task", "required": True}], []),
            "task.list": ("Tasks visible to you", [], [{"name": "state", "type": "string"}]),
            "scenario.list": ("List scenarios", [], []),
            "scenario.show": ("Show one scenario with its steps", [{"name": "name", "type": "string", "required": True}], []),
        }
        summary, args, flags = table[p]
        base.update(summary=summary, args=args, flags=flags)
        if p == "search":
            base["examples"] = [{"argv": ["search", "compare runs"]}]
        if p == "explore":
            base["examples"] = [{"argv": ["explore", "optimize a wing"]}]
        return base

    def group_children(self, group: str) -> list[dict] | None:
        if group in self.app["groups"]:
            out = []
            for n, c in self.app["groups"][group]["commands"].items():
                item = {"name": n, "kind": "command", "summary": c["summary"]}
                if c.get("effects"): item["effects"] = c["effects"]
                out.append(item)
            return out
        if group in self.RESERVED:
            return [{"name": n, "kind": "command", "summary": s} for n, s in self.RESERVED[group].items()]
        return None

    def all_paths(self) -> list[list[str]]:
        out: list[list[str]] = []
        for c in self.root_children():
            if c["kind"] == "command":
                out.append([c["name"]])
            else:
                out += [[c["name"], x["name"]] for x in self.group_children(c["name"]) or []]
        return out

    def resolve(self, argv: list[str]) -> tuple[list[str], str, list[str]]:
        """Returns (path, kind, rest). kind in {command, group, none}."""
        if not argv:
            return [], "none", []
        first = argv[0]
        if self.descriptor([first]):
            return [first], "command", argv[1:]
        if self.group_children(first) is not None:
            if len(argv) > 1 and self.descriptor([first, argv[1]]):
                return [first, argv[1]], "command", argv[2:]
            return [first], "group", argv[1:]
        return [], "none", argv

    # ---- public entry
    def invoke(self, request: dict) -> dict:
        resp = self._invoke(request)
        if "id" in request:
            resp["id"] = request["id"]
        self.log.append({"request": request, "response": resp})
        return resp

    def _invoke(self, request: dict) -> dict:
        command = ""
        try:
            if not isinstance(request, dict) or ("argv" in request) == ("line" in request):
                raise AciError("bad_request", "Exactly one of argv/line is required.", "Send {\"line\": \"help\"}.")
            if request.get("aci", ACI_VERSION).split(".")[0] != ACI_VERSION.split(".")[0]:
                raise AciError("unsupported_version", "Unsupported protocol version.", "Use aci 0.2.", details={"supported": [ACI_VERSION]})
            if "line" in request:
                argv = lex_line(str(request["line"]).strip(), self.app["name"])
            else:
                argv = [str(t) for t in request["argv"]]
            if not argv:
                raise AciError("bad_request", "Empty command.", "Send `help` to list commands.")
            options = dict(request.get("options") or {})
            path, kind, rest = self.resolve(argv)
            command = ".".join(path)
            if kind == "none":
                raise self._unknown(argv[0], [" ".join(p) for p in self.all_paths()], ["help"])
            if kind == "group":
                if rest and not rest[0].startswith("-"):
                    names = [c["name"] for c in self.group_children(path[0]) or []]
                    raise self._unknown(rest[0], [f"{path[0]} {n}" for n in names], ["help", path[0]])
                return self._ok(command, **self._help_node(path, options))
            desc = self.descriptor(path)
            assert desc
            parsed = self._parse(desc, rest, request.get("input"), options)
            if parsed["globals"].get("help"):
                return self._ok(command, **self._help_descriptor(desc, parsed["globals"]))
            return self._execute(desc, parsed, command)
        except AciError as e:
            return self._err(command, e)

    # ---- envelopes
    def _ok(self, command: str, summary: str, **fields: Any) -> dict:
        r = {"aci": ACI_VERSION, "ok": True, "command": command, "summary": summary}
        r.update({k: v for k, v in fields.items() if v is not None})
        return r

    def _err(self, command: str, e: AciError) -> dict:
        err = {"code": e.code, "message": e.message, "hint": e.hint}
        err.update(e.extra)
        return {"aci": ACI_VERSION, "ok": False, "command": command, "error": err}

    def _unknown(self, token: str, candidates: list[str], help_argv: list[str]) -> AciError:
        close = difflib.get_close_matches(token, candidates, n=3, cutoff=0.5)
        return AciError("unknown_command", f"Unknown command '{token}'.",
                        "Run `help` to list commands or `search \"<intent>\"` to find one.",
                        did_you_mean=[c.split(" ") for c in close] or None, help=help_argv)

    # ---- argv parsing (§4.3) and coercion (§4.4.1)
    def _parse(self, desc: dict, rest: list[str], inp: Any, options: dict) -> dict:
        flags_by_name = {f["name"]: f for f in desc["flags"]}
        shorts = {f["short"]: f for f in desc["flags"] if f.get("short")}
        values: dict[str, Any] = {}
        globals_: dict[str, Any] = {}
        positionals: list[str] = []
        i, only_pos = 0, False

        def take_value(name: str, tok_iter_pos: int) -> tuple[Any, int]:
            if tok_iter_pos < len(rest):
                return rest[tok_iter_pos], tok_iter_pos + 1
            raise AciError("missing_arg", f"Flag --{name} needs a value.", f"Pass --{name} <value>.",
                           details={"missing": [name]}, fix=[[*desc["path"], f"--{name}", "<value>"]])

        while i < len(rest):
            tok = rest[i]
            if only_pos or not tok.startswith("-") or re.match(r"^-[0-9]", tok):
                positionals.append(tok); i += 1; continue
            if tok == "--":
                only_pos = True; i += 1; continue
            if tok.startswith("--"):
                body = tok[2:]
                name, eq, val = body.partition("=")
                neg = False
                if name.startswith("no-") and name[3:] in flags_by_name and flags_by_name[name[3:]]["type"] == "bool":
                    name, neg = name[3:], True
                i += 1
                if name in GLOBAL_FLAGS:
                    if name in BOOL_GLOBALS:
                        globals_[name] = (val.lower() not in ("false", "0", "no")) if eq else True
                    else:
                        v, i = (val, i) if eq else take_value(name, i)
                        globals_[name] = v
                    continue
                f = flags_by_name.get(name)
                if not f:
                    close = difflib.get_close_matches(name, list(flags_by_name) + sorted(GLOBAL_FLAGS), n=3, cutoff=0.5)
                    raise AciError("invalid_args", f"Unknown flag --{name}.", f"See `{' '.join(desc['path'])} --help` for the accepted flags.",
                                   did_you_mean=[[f"--{c}"] for c in close] or None, help=[*desc["path"], "--help"])
                if f["type"] == "bool":
                    v: Any = (val.lower() not in ("false", "0", "no")) if eq else True
                    if neg: v = False
                else:
                    if neg:
                        raise AciError("invalid_args", f"--no-{name} is only valid for boolean flags.", f"Pass --{name} <value>.", help=[*desc["path"], "--help"])
                    v, i = (val, i) if eq else take_value(name, i)
                self._store(f, values, v, desc)
                continue
            # short alias
            letter, attached = tok[1], tok[2:]
            f = shorts.get(letter)
            if not f or len(tok) > 2 and f["type"] == "bool":
                raise AciError("invalid_args", f"Unknown or bundled short flag {tok}.", "Short flags cannot be bundled; use the long form.",
                               help=[*desc["path"], "--help"])
            i += 1
            if f["type"] == "bool":
                v = True
            else:
                v, i = (attached, i) if attached else take_value(f["name"], i)
            self._store(f, values, v, desc)

        if globals_.get("help"):
            return {"values": values, "globals": globals_}

        # positionals
        args = desc["args"]
        for idx, tok in enumerate(positionals):
            if idx < len(args):
                a = args[idx]
                if a.get("repeatable"):
                    values.setdefault(a["name"], []).append(tok)
                else:
                    values[a["name"]] = tok
            elif args and args[-1].get("repeatable"):
                values.setdefault(args[-1]["name"], []).append(tok)
            else:
                raise AciError("invalid_args", f"Unexpected argument '{tok}'.", f"See `{' '.join(desc['path'])} --help`.", help=[*desc["path"], "--help"])

        # required + defaults + substitution + coercion
        for p in args + desc["flags"]:
            n = p["name"]
            if n not in values:
                if p.get("required"):
                    raise AciError("missing_arg", f"Required {'flag --' if p in desc['flags'] else 'argument '}{n} is missing.",
                                   f"See `{' '.join(desc['path'])} --help` for usage.", details={"missing": [n]},
                                   fix=[[*desc["path"], f"--{n}" if p in desc["flags"] else "", f"<{n}>"]], help=[*desc["path"], "--help"])
                if "default" in p:
                    values[n] = p["default"]
                continue
            values[n] = self._coerce(p, self._substitute(values[n], inp, n), n)
        return {"values": values, "globals": globals_}

    def _store(self, f: dict, values: dict, v: Any, desc: dict) -> None:
        if f.get("repeatable"):
            values.setdefault(f["name"], []).append(v)
        elif f["name"] in values:
            raise AciError("invalid_args", f"Flag --{f['name']} given more than once.", "Pass it once.", help=[*desc["path"], "--help"])
        else:
            values[f["name"]] = v

    def _substitute(self, v: Any, inp: Any, flag: str) -> Any:
        if isinstance(v, list):
            return [self._substitute(x, inp, flag) for x in v]
        if not isinstance(v, str):
            return v
        if v.startswith("@@"):
            return v[1:]
        if v == "@input" or v.startswith("@input."):
            if inp is None:
                raise AciError("invalid_value", f"{v} references input, but the request has no input.",
                               "Add an `input` object to the call and reference its keys as @input.<key>.",
                               details={"flag": flag, "ref": v[1:]})
            cur = inp
            for part in re.findall(r"[^.\[\]]+|\[\d+\]", v[len("@input"):]):
                try:
                    cur = cur[int(part[1:-1])] if part.startswith("[") else cur[part]
                except (KeyError, IndexError, TypeError):
                    raise AciError("invalid_value", f"{v} does not resolve in input.", "Check the key name in your `input` object.",
                                   details={"flag": flag, "ref": v[1:]})
            return cur
        return v

    def _coerce(self, p: dict, v: Any, flag: str) -> Any:
        if isinstance(v, list) and p.get("repeatable"):
            return [self._coerce({**p, "repeatable": False}, x, flag) for x in v]
        last_err: AciError | None = None
        for t in p["type"].split("|"):
            try:
                return self._coerce_one(t, p, v, flag)
            except AciError as e:
                last_err = e
        assert last_err
        raise last_err

    def _coerce_one(self, t: str, p: dict, v: Any, flag: str) -> Any:
        bad = lambda expected: AciError("invalid_value", f"Value {v!r} for {flag} is not a valid {expected}.",
                                        f"Expected {expected}.", details={"flag": flag, "expected": expected})
        if t == "string":
            if isinstance(v, str): return v
            raise bad("string")
        if t == "int":
            if isinstance(v, bool): raise bad("int")
            if isinstance(v, int): return v
            if isinstance(v, str) and re.match(r"^[+-]?[0-9]+$", v): return int(v)
            raise bad("int")
        if t == "number":
            if isinstance(v, (int, float)) and not isinstance(v, bool): return v
            try: return float(v)
            except (TypeError, ValueError): raise bad("number")
        if t == "bool":
            if isinstance(v, bool): return v
            if isinstance(v, str) and v.lower() in ("true", "1", "yes"): return True
            if isinstance(v, str) and v.lower() in ("false", "0", "no"): return False
            raise bad("bool")
        if t == "enum":
            if v in p["values"]: return v
            close = difflib.get_close_matches(str(v), p["values"], n=3, cutoff=0.4)
            raise AciError("invalid_value", f"Value {v!r} for {flag} is not one of {p['values']}.", "Use one of the listed values.",
                           details={"flag": flag, "expected": "|".join(p["values"])}, did_you_mean=[[c] for c in close] or None)
        if t == "json":
            if isinstance(v, str):
                try: return json.loads(v)
                except ValueError:
                    raise AciError("invalid_value", f"Value for {flag} is not valid JSON.",
                                   "Put the value in `input` and reference it as @input.<key>.", details={"flag": flag, "expected": "json"})
            return v
        if t.startswith("handle:"):
            want = t.split(":", 1)[1]
            hid = v.get("id") if isinstance(v, dict) else v
            if not isinstance(hid, str): raise bad(t)
            got = self.handle_type(hid)
            if got is None:
                names = list(self.simulations) + list(self.workflows) + list(self.runs) + list(self.tasks) + list(self.optimizations)
                names += [s["name"] for s in self.simulations.values()] + [w["name"] for w in self.workflows.values()]
                close = difflib.get_close_matches(hid, names, n=3, cutoff=0.5)
                raise AciError("not_found", f"No {want} with id or name '{hid}'.", f"List available {want}s first.",
                               details={"ref": hid}, did_you_mean=[[c] for c in close] or None)
            if got != want:
                raise AciError("invalid_value", f"'{hid}' is a {got}, not a {want}.", f"Pass a {want} handle.",
                               details={"flag": flag, "expected": t, "got": f"handle:{got}"})
            return hid
        if t == "path":
            if isinstance(v, str) and ".." not in v.split("/"): return v
            raise bad("path")
        raise bad(t)

    # ---- help rendering
    def _help_node(self, path: list[str], options: dict) -> dict:
        fmt = options.get("format", "text")
        if path:
            children = self.group_children(path[0]) or []
            summary_line = f"{' '.join(path)} — {(self.app['groups'].get(path[0]) or {}).get('summary', '')}"
        else:
            children = self.root_children()
            summary_line = f"{self.app['name']} — {self.app['description']}"
        node = {"path": path, "kind": "group", "summary": summary_line.split(" — ")[1], "children": children}
        summary = f"{' '.join(path) or self.app['name']}: {sum(c['kind']=='group' for c in children)} groups, {sum(c['kind']=='command' for c in children)} commands"
        if fmt == "json":
            return {"summary": summary, "data": node}
        lines = [summary_line, ""]
        groups = [c for c in children if c["kind"] == "group"]
        cmds = [c for c in children if c["kind"] == "command"]
        if groups:
            lines.append("Groups:")
            lines += [f"  {c['name']:<14}{c['summary']}" for c in groups]
        if cmds:
            lines.append("Commands:")
            lines += [f"  {c['name']:<14}{c['summary']}" + (f"  [{', '.join(c['effects'])}]" if c.get("effects") else "") for c in cmds]
        if not path:
            lines.append(f"\nScenarios (top): {', '.join(self.app['top_scenarios'])}")
        lines.append("Use `help <group>` to go deeper, `<command> --help` for a contract.")
        return {"summary": summary, "text": "\n".join(lines)}

    def _help_descriptor(self, desc: dict, globals_: dict) -> dict:
        if globals_.get("format") == "json":
            return {"summary": desc["summary"], "data": desc}
        p = " ".join(desc["path"])
        usage = [p] + [f"<{a['name']}>" if a.get("required") else f"[{a['name']}]" for a in desc["args"]]
        for f in desc["flags"]:
            t = "|".join(f["values"]) if f["type"] == "enum" else f["type"]
            piece = f"--{f['name']}" + ("" if f["type"] == "bool" else f" <{t}>")
            usage.append(piece if f.get("required") else f"[{piece}]")
        lines = [f"{p} — {desc['summary']}", "", "Usage:", "  " + " ".join(usage)]
        if desc["args"]:
            lines += ["", "Arguments:"] + [f"  {a['name']:<12}<{a['type']}>  {a.get('help', '')}" for a in desc["args"]]
        if desc["flags"]:
            lines += ["", "Flags:"]
            for f in desc["flags"]:
                t = "|".join(f["values"]) if f["type"] == "enum" else f["type"]
                extra = " (required)" if f.get("required") else (f" Default: {f['default']}." if "default" in f else "")
                lines.append(f"  --{f['name']:<12}<{t}>  {f.get('help', '')}{extra}")
        lines += ["", f"Effects: {', '.join(desc['effects']) or 'none (read-only)'}   Async: {'yes' if desc.get('async') else 'no'}"]
        if desc.get("preconditions"):
            lines += ["", "Preconditions:"] + [f"  - {c['text']}   check: {' '.join(c['check'])}" for c in desc["preconditions"]]
        lines += ["", "Examples:"]
        for ex in desc.get("examples", []):
            lines.append("  " + " ".join(ex["argv"]))
            if ex.get("input"): lines.append(f"      input: {json.dumps(ex['input'])}")
            if ex.get("comment"): lines.append(f"      {ex['comment']}")
        if desc.get("errors"):
            lines += ["", "Errors:"] + [f"  {e['code']:<22}{e['when']}   fix: {' '.join(e['fix'])}" for e in desc["errors"]]
        return {"summary": desc["summary"], "text": "\n".join(lines)}

    # ---- execution
    def _execute(self, desc: dict, parsed: dict, command: str) -> dict:
        v, g = parsed["values"], parsed["globals"]
        effects = desc["effects"]
        plan = None
        if g.get("dry-run"):
            plan = self._plan(command, v) if effects else "read-only; no effects"
            return self._ok(command, f"Dry run: {plan}", data={"plan": plan})
        if ("destructive" in effects or "billing" in effects) and not g.get("yes"):
            plan = self._plan(command, v)
            fix = [*desc["path"]] + [v[a["name"]] for a in desc["args"]] + ["--yes"]
            raise AciError("confirmation_required", plan, "Confirm with the user, then re-run with --yes.",
                           details={"plan": plan}, fix=[fix])
        handler = getattr(self, "cmd_" + command.replace(".", "_"))
        result = handler(v, g)
        resp = self._ok(command, **result)
        if g.get("quiet"):
            for k in ("next", "warnings", "text"): resp.pop(k, None)
        return resp

    def _plan(self, command: str, v: dict) -> str:
        if command == "workflow.delete":
            wf = self.workflows[v["workflow"]] if v["workflow"] in self.workflows else None
            n = len(wf["runs"]) if wf else 0
            return f"Deleting workflow {v['workflow']} removes it and {n} run(s) with their results."
        if command == "workflow.run":
            return f"Would start a run of workflow {v['workflow']} (priority {v.get('priority')})."
        if command == "simulation.import":
            return f"Would import {v['path']} as model '{v['name']}' ({v.get('kind')})."
        if command == "optimization.configure":
            return f"Would create an optimization study on {v['model']} with {len(v['vars'])} variables."
        return f"Would execute {command}."

    # -- reserved
    def cmd_info(self, v, g):
        m = self.manifest()
        return {"summary": f"{m['name']} {m['version']} — {m['description']}", "data": m,
                "text": f"{m['name']} {m['version']} — {m['description']}\ntop scenarios: {', '.join(m['top_scenarios'])}"}

    def cmd_help(self, v, g):
        path = v.get("path") or []
        if path and self.descriptor(path):
            return self._help_descriptor(self.descriptor(path), g)  # type: ignore[arg-type]
        if path and self.group_children(path[0]) is None:
            raise self._unknown(path[0], [" ".join(p) for p in self.all_paths()], ["help"])
        return self._help_node(path[:1], {"format": g.get("format", "text")})

    def _search_hits(self, query: str, kind: str) -> list[dict]:
        words = [w for w in re.findall(r"[a-z0-9_]+", query.lower()) if len(w) > 2]
        hits = []
        if kind in ("command", "all"):
            for path in self.all_paths():
                d = self.descriptor(path)
                if not d: continue
                hay = " ".join([" ".join(path), d["summary"], d.get("description", "")] + [e.get("comment", "") for e in d.get("examples", [])]).lower()
                score = sum(hay.count(w) for w in words) + 2 * sum(w in path for w in words)
                if score:
                    hits.append({"kind": "command", "path": path, "summary": d["summary"], "score": score, "matched": ["summary"]})
        if kind in ("scenario", "all"):
            for s in self.app["scenarios"]:
                hay = " ".join([s["title"], s["summary"]] + s["intents"]).lower()
                score = sum(hay.count(w) for w in words)
                if score:
                    hits.append({"kind": "scenario", "name": s["name"], "summary": s["summary"], "score": score + 1, "matched": ["intent"]})
        hits.sort(key=lambda h: -h["score"])
        return hits

    def cmd_search(self, v, g):
        limit = int(g.get("limit", 10))
        hits = self._search_hits(" ".join(v["query"]), v.get("kind", "all"))[:limit]
        lines = [f"{h['kind']:<9}{' '.join(h['path']) if h['kind']=='command' else h['name']:<28}{h['summary']}" for h in hits]
        summary = f"{len(hits)} hit(s)" + (f"; top: {lines[0].split()[1] if hits[0]['kind']=='scenario' else ' '.join(hits[0]['path'])}" if hits else "")
        out = {"summary": summary, "data": {"hits": hits}, "text": "\n".join(lines) or "No matches."}
        if not hits:
            out["next"] = [{"argv": ["help"], "why": "Browse the command tree."}]
        return out

    def _scenario_text(self, s: dict) -> str:
        lines = [f"Scenario {s['name']} — {s['title']}", f"  {s['summary']}"]
        if s.get("prerequisites"):
            lines += ["  prerequisites:"] + [f"    - {p['text']}   check: {' '.join(p['check'])}" for p in s["prerequisites"] if p.get("check")]
        if s.get("placeholders"):
            lines.append("  placeholders: " + ", ".join(f"{p['name']} ({'from user' if p['from']=='user' else 'from ' + p['from'].replace(':', ' ')})" for p in s["placeholders"]))
        lines.append("  steps:")
        for i, st in enumerate(s["steps"], 1):
            lines.append(f"    {i} {' '.join(st['do'])}" + ("   (async)" if st.get("async") else "") + (f"   → {st['expect']}" if st.get("expect") else ""))
        return "\n".join(lines)

    def cmd_explore(self, v, g):
        hits = self._search_hits(" ".join(v["goal"]), "scenario")[:3]
        if not hits:
            return {"summary": "No matching scenario.", "data": {"scenarios": []}, "text": "No matching scenario. Try `help`.",
                    "next": [{"argv": ["help"], "why": "Browse the tree."}]}
        top = next(s for s in self.app["scenarios"] if s["name"] == hits[0]["name"])
        rest = [{"name": h["name"], "title": next(s["title"] for s in self.app["scenarios"] if s["name"] == h["name"]), "score": h["score"]} for h in hits[1:]]
        text = self._scenario_text(top) + ("".join(f"\nAlso: {r['name']} — {r['title']}" for r in rest))
        return {"summary": f"Best match: {top['name']} ({len(top['steps'])} steps).", "data": {"scenarios": [top, *rest]}, "text": text}

    def cmd_scenario_list(self, v, g):
        items = [{"name": s["name"], "title": s["title"], "summary": s["summary"]} for s in self.app["scenarios"]]
        return {"summary": f"{len(items)} scenario(s).", "data": {"scenarios": items},
                "text": "\n".join(f"{s['name']:<20}{s['title']}" for s in items)}

    def cmd_scenario_show(self, v, g):
        s = next((s for s in self.app["scenarios"] if s["name"] == v["name"]), None)
        if not s:
            close = difflib.get_close_matches(v["name"], [s["name"] for s in self.app["scenarios"]], n=3, cutoff=0.4)
            raise AciError("not_found", f"No scenario '{v['name']}'.", "Run `scenario list`.", details={"ref": v["name"]},
                           did_you_mean=[[c] for c in close] or None)
        return {"summary": f"{s['name']}: {len(s['steps'])} steps.", "data": s, "text": self._scenario_text(s)}

    # -- tasks
    def _task_view(self, t: dict) -> dict:
        out = {"id": t["id"], "state": t["state"]}
        if t.get("progress"): out["progress"] = t["progress"]
        if t.get("result"): out["result"] = t["result"]
        return out

    def _advance(self, t: dict) -> None:
        if t["state"] in ("succeeded", "failed", "cancelled"):
            return
        t["ticks"] += 1
        if t["ticks"] == 1:
            t["state"], t["progress"] = "running", {"percent": 40, "message": "generation 8/20"}
        else:
            run = self.runs[t["run"]]
            run["state"] = "succeeded"
            t["state"], t["progress"] = "succeeded", {"percent": 100, "message": "done"}
            t["result"] = self._ok("workflow.run", f"Run {run['id']} finished: 20 generations, 9 Pareto points.",
                                   data={"run": run["id"], "pareto_points": 9},
                                   handles=[{"id": run["id"], "type": "run", "label": run["workflow"], "expires": None}],
                                   next=[{"argv": ["results", "pareto", run["id"], "--explain"], "why": "Inspect the Pareto front."}])

    def _task_response(self, t: dict) -> dict:
        view = self._task_view(t)
        summary = f"Task {t['id']} {t['state']}" + (f" {t['progress']['percent']}% \"{t['progress']['message']}\"" if t.get("progress") and t["state"] == "running" else "")
        out: dict = {"summary": summary, "task": view}
        if t["state"] == "succeeded":
            out["summary"] = f"Task {t['id']} succeeded — {t['result']['summary']}"
            out["handles"] = t["result"]["handles"]; out["next"] = t["result"]["next"]
        elif t["state"] in ("queued", "running"):
            out["next"] = [{"argv": ["task", "wait", t["id"]], "why": "Wait for completion."}]
        return out

    def cmd_task_status(self, v, g):
        return self._task_response(self.tasks[v["id"]])

    def cmd_task_wait(self, v, g):
        t = self.tasks[v["id"]]; self._advance(t)
        return self._task_response(t)

    def cmd_task_cancel(self, v, g):
        t = self.tasks[v["id"]]
        if t["state"] in ("succeeded", "failed", "cancelled"):
            raise AciError("conflict", f"Task {t['id']} already {t['state']}.", "Nothing to cancel.")
        t["state"] = "cancelled"
        return {"summary": f"Task {t['id']} cancelled.", "task": self._task_view(t)}

    def cmd_task_logs(self, v, g):
        t = self.tasks[v["id"]]
        lines = ["[00:00] queued", "[00:01] running"] + (["[03:58] done"] if t["state"] == "succeeded" else [])
        return {"summary": f"{len(lines)} log lines.", "data": {"lines": lines}, "text": "\n".join(lines)}

    def cmd_task_list(self, v, g):
        items = [self._task_view(t) for t in self.tasks.values() if not v.get("state") or t["state"] == v["state"]]
        return {"summary": f"{len(items)} task(s).", "data": {"tasks": items},
                "text": "\n".join(f"{t['id']}  {t['state']}" for t in items) or "No tasks."}

    # -- domain
    def cmd_simulation_list(self, v, g):
        items = [{"id": k, **{x: s[x] for x in ("name", "kind")}} for k, s in self.simulations.items() if not v.get("kind") or s["kind"] == v["kind"]]
        kind = f"{v['kind'].upper()} " if v.get("kind") else ""
        return {"summary": f"{len(items)} {kind}model(s): " + ", ".join(f"{i['name']} ({i['id']})" for i in items) + ".",
                "data": {"models": items},
                "handles": [{"id": i["id"], "type": "simulation", "label": i["name"], "expires": None} for i in items],
                "text": "\n".join(f"{i['id']:<14}{i['name']:<14}{i['kind']}" for i in items) or "No models."}

    def _find_sim(self, ref: str) -> str:
        if ref in self.simulations: return ref
        for k, s in self.simulations.items():
            if s["name"] == ref: return k
        names = list(self.simulations) + [s["name"] for s in self.simulations.values()]
        close = difflib.get_close_matches(ref, names, n=3, cutoff=0.5)
        raise AciError("not_found", f"No simulation with id or name '{ref}'.", "Run `simulation list` to see available models.",
                       details={"ref": ref}, did_you_mean=[["simulation", "inspect", c] for c in close] or None, fix=[["simulation", "list"]])

    def cmd_simulation_inspect(self, v, g):
        sid = self._find_sim(v["model"]); s = self.simulations[sid]
        data = {"id": sid, "name": s["name"], "kind": s["kind"]}
        text = f"{sid}  {s['name']}  kind={s['kind']}"
        if v.get("parameters"):
            data["parameters"] = s["parameters"]
            text += "\nParameters:\n" + "\n".join(f"  {p['name']:<10}{p['type']:<8}range {p['range']}" for p in s["parameters"])
        return {"summary": f"{s['name']} ({sid}) is a {s['kind'].upper()} model with {len(s['parameters'])} parameters: " + ", ".join(p["name"] for p in s["parameters"]) + ".",
                "data": data, "handles": [{"id": sid, "type": "simulation", "label": s["name"], "expires": None}], "text": text,
                "next": [{"argv": ["optimization", "configure", "--model", sid, "--vars", "@input.vars", "--objectives", "@input.objectives"], "why": "Set up an optimization on this model."}]}

    def cmd_simulation_import(self, v, g):
        sid = self._next("sim")
        self.simulations[sid] = {"name": v["name"], "kind": v.get("kind", "cfd"), "parameters": [{"name": "chord", "type": "number", "range": [0.8, 1.4]}]}
        return {"summary": f"Imported '{v['name']}' from {v['path']} as {sid} ({v.get('kind', 'cfd')}).",
                "data": {"id": sid}, "handles": [{"id": sid, "type": "simulation", "label": v["name"], "expires": None}],
                "next": [{"argv": ["simulation", "inspect", sid, "--parameters"], "why": "See its parameters."}]}

    def cmd_workflow_list(self, v, g):
        items = [{"id": k, "name": w["name"], "validated": w["validated"], "runs": len(w["runs"])} for k, w in self.workflows.items()]
        return {"summary": f"{len(items)} workflow(s): " + ", ".join(f"{i['name']} ({i['id']})" for i in items) + ".", "data": {"workflows": items},
                "handles": [{"id": i["id"], "type": "workflow", "label": i["name"], "expires": None} for i in items],
                "text": "\n".join(f"{i['id']:<12}{i['name']:<12}validated={i['validated']} runs={i['runs']}" for i in items) or "No workflows."}

    def _find_wf(self, ref: str) -> str:
        if ref in self.workflows: return ref
        for k, w in self.workflows.items():
            if w["name"] == ref: return k
        close = difflib.get_close_matches(ref, list(self.workflows) + [w["name"] for w in self.workflows.values()], n=3, cutoff=0.5)
        raise AciError("not_found", f"No workflow with id or name '{ref}'.", "Run `workflow list`.", details={"ref": ref},
                       did_you_mean=[[c] for c in close] or None, fix=[["workflow", "list"]])

    def cmd_workflow_validate(self, v, g):
        wid = self._find_wf(v["workflow"]); w = self.workflows[wid]
        w["validated"] = True
        warnings = ["'twist' has no bounds; defaults to ±5°."] if w.get("optimization") else []
        return {"summary": f"Workflow {wid} is valid." + (f" {len(warnings)} warning." if warnings else ""), "data": {"valid": True, "issues": []},
                "warnings": warnings, "next": [{"argv": ["workflow", "run", "--workflow", wid], "why": "Run it."}]}

    def cmd_workflow_run(self, v, g):
        wid = self._find_wf(v["workflow"]); w = self.workflows[wid]
        if not w["validated"]:
            raise AciError("precondition_failed", f"Workflow {wid} has not been validated.", "Validate it first, then run again.",
                           fix=[["workflow", "validate", wid]], help=["workflow", "run", "--help"])
        rid, tid = self._next("r"), self._next("t")
        self.runs[rid] = {"id": rid, "workflow": wid, "state": "running"}
        w["runs"].append(rid)
        self.tasks[tid] = {"id": tid, "state": "queued", "ticks": 0, "run": rid, "progress": None, "result": None}
        return {"summary": f"Started run {rid} of workflow {wid} (est. 4 min).", "data": {"run": rid, "estimate_s": 240},
                "handles": [{"id": rid, "type": "run", "label": wid, "expires": None}], "task": {"id": tid, "state": "queued"},
                "next": [{"argv": ["task", "wait", tid], "why": "Block until the run completes."}]}

    def cmd_workflow_delete(self, v, g):
        wid = self._find_wf(v["workflow"]); w = self.workflows.pop(wid)
        for rid in w["runs"]: self.runs.pop(rid, None)
        return {"summary": f"Deleted workflow {wid} and {len(w['runs'])} run(s).", "data": {"deleted": wid}}

    def cmd_optimization_configure(self, v, g):
        sid = v["model"]; s = self.simulations[sid]
        vars_ = v["vars"]; objs = v["objectives"]
        if not isinstance(vars_, list) or not all(isinstance(x, str) for x in vars_):
            raise AciError("invalid_value", "--vars must be a JSON array of parameter names.", "Example: @input.vars with input {\"vars\": [\"chord\",\"twist\"]}.",
                           details={"flag": "vars", "expected": "array of string"})
        unknown = [x for x in vars_ if x not in {p["name"] for p in s["parameters"]}]
        if unknown:
            raise AciError("invalid_value", f"Unknown parameter(s) {unknown} for model {sid}.", "Check parameter names with `simulation inspect <model> --parameters`.",
                           details={"flag": "vars", "expected": [p["name"] for p in s["parameters"]]}, fix=[["simulation", "inspect", sid, "--parameters"]])
        if not isinstance(objs, list) or not all(isinstance(o, dict) and "name" in o for o in objs):
            raise AciError("invalid_value", "--objectives must be a JSON array of {name, goal}.", "Example: [{\"name\":\"lift_drag\",\"goal\":\"max\"}].",
                           details={"flag": "objectives", "expected": "array of {name, goal}"})
        oid, wid = self._next("opt"), None
        wid = f"wf_{oid}"
        self.optimizations[oid] = {"model": sid, "vars": vars_, "objectives": objs, "workflow": wid}
        self.workflows[wid] = {"name": wid, "validated": False, "runs": [], "optimization": oid}
        return {"summary": f"Created optimization study {oid} ({len(vars_)} variables, {len(objs)} objective(s), algorithm {v.get('algorithm')}). Workflow {wid} generated; validate it before running.",
                "data": {"optimization": oid, "workflow": wid},
                "handles": [{"id": oid, "type": "optimization", "label": sid, "expires": None}, {"id": wid, "type": "workflow", "label": oid, "expires": None}],
                "next": [{"argv": ["workflow", "validate", wid], "why": "Required before running."}]}

    def cmd_results_pareto(self, v, g):
        run = self.runs[v["run"]]
        if run["state"] != "succeeded":
            raise AciError("precondition_failed", f"Run {run['id']} has not finished.", "Wait for the task first.", fix=[["task", "list"]])
        rows = [{"chord": 1.12, "twist": 2.1, "lift_drag": 18.4}, {"chord": 1.05, "twist": 1.4, "lift_drag": 17.9}, {"chord": 1.20, "twist": 3.0, "lift_drag": 17.6}]
        limit = min(int(g.get("limit", DEFAULT_LIMIT)), len(rows))
        return {"summary": f"9 Pareto points; best lift/drag {rows[0]['lift_drag']} at chord={rows[0]['chord']}, twist=+{rows[0]['twist']}°.",
                "data": {"rows": rows[:limit]}, "truncated": {"rows": {"shown": limit, "total": 9, "cursor": "c3"}},
                "text": "chord  twist  lift_drag\n" + "\n".join(f"{r['chord']:<6} {r['twist']:<6} {r['lift_drag']}" for r in rows[:limit])}


# ---------------------------------------------------------------- rendering

def render_text(resp: dict) -> str:
    """Host-side rendering of an envelope for models that read plain text."""
    if not resp.get("ok"):
        e = resp["error"]
        lines = [f"error {e['code']}: {e['message']}", f"hint: {e['hint']}"]
        for fix in e.get("fix") or []:
            lines.append("fix: " + " ".join(fix))
        if e.get("did_you_mean"):
            lines.append("did you mean: " + " | ".join(" ".join(d) for d in e["did_you_mean"]))
        if e.get("help"):
            lines.append("help: " + " ".join(e["help"]))
        return "\n".join(lines)
    lines = [f"ok: {resp['summary']}"]
    if resp.get("text"):
        lines.append(resp["text"])
    elif resp.get("data") is not None:
        lines.append("data: " + json.dumps(resp["data"], ensure_ascii=False))
    if resp.get("handles"):
        lines.append("handles: " + ", ".join(f"{h['id']} ({h['type']})" for h in resp["handles"]))
    if resp.get("task"):
        t = resp["task"]; lines.append(f"task: {t['id']} {t['state']}")
    if resp.get("truncated"):
        for k, tr in resp["truncated"].items():
            lines.append(f"truncated: {k} shown {tr['shown']} of {tr['total']}")
    if resp.get("warnings"):
        lines += [f"warning: {w}" for w in resp["warnings"]]
    if resp.get("next"):
        lines.append("next: " + " | ".join(" ".join(n["argv"]) for n in resp["next"]))
    return "\n".join(lines)
