"""Run an LLM against the mock AAI application and score it.

    python tests/llm_eval.py --provider anthropic --model claude-opus-5
    python tests/llm_eval.py --provider openai --model gpt-4o-mini
    python tests/llm_eval.py --provider openrouter --model qwen/qwen3-32b
    python tests/llm_eval.py --provider openai --model llama3.1:8b --base-url http://localhost:11434/v1
    python tests/llm_eval.py --provider scripted            # harness self-check, no API key

The agent sees exactly what SPEC.md §5.4 prescribes: one tool whose
description is the bootstrap text, plus the system prompt from
AGENT_PROMPT.md. Results are rendered as text (host choice, P10) unless
--format json is given. A JSON report is written to tests/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).parent))
from aai_mock import MockServer, render_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HARD_VIOLATIONS = ("yes_before_confirm", "placeholder_sent")


# ---------------------------------------------------------------- prompt/tool

def load_system_prompt(server: MockServer) -> str:
    text = (ROOT / "AGENT_PROMPT.md").read_text()
    body = text.split("\n---\n", 1)[1].strip()
    m = server.manifest()
    return (body.replace("{tool}", m["name"]).replace("{title}", m["title"])
                .replace("{top_scenarios}", ", ".join(m["top_scenarios"])))


def bootstrap_text(server: MockServer) -> str:
    m = server.manifest()
    return (
        f'You can use the application "{m["name"]}" by sending one command line at a time.\n'
        "- `help` lists top-level groups and commands; `help <group>` lists a group.\n"
        "- `<command> --help` shows arguments, examples, side effects and permissions.\n"
        '- `search "<what you want to do>"` finds commands and scenarios by intent.\n'
        '- `explore "<goal>"` returns a step-by-step scenario to follow.\n'
        "- Long operations return a task id: `task wait <id>`, `task logs <id>`.\n"
        "- Pass large or structured values via `input` and reference them as @input.<key>.\n"
        "Every result has a `summary`, optional `data`, `handles` you can pass to other commands, "
        "and `next` suggestions. Errors include a `hint` and often a `fix` argv you can run directly. "
        "Destructive commands need `--yes`; use `--dry-run` to preview."
    )


def tool_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "line": {"type": "string", "description": 'The command as one line, e.g. "workflow run --workflow wing". Preferred.'},
            "argv": {"type": "array", "items": {"type": "string"}, "description": "Alternative to line: pre-split tokens."},
            "input": {"type": "object", "description": "Structured values referenced from the command as @input.<key>."},
        },
    }


# ---------------------------------------------------------------- adapters

@dataclass
class ToolCall:
    id: str
    args: dict


@dataclass
class Step:
    text: str | None
    tool_calls: list[ToolCall]


class Adapter(Protocol):
    def start(self, system: str, tool_name: str, tool_description: str, schema: dict) -> None: ...
    def user(self, text: str) -> None: ...
    def tool_results(self, results: list[tuple[str, str, bool]]) -> None: ...
    def step(self) -> Step: ...


class AnthropicAdapter:
    def __init__(self, model: str):
        import anthropic  # official SDK; optional dependency
        self.client = anthropic.Anthropic()
        self.model = model
        self.messages: list[dict] = []

    def start(self, system, tool_name, tool_description, schema):
        self.system = system
        self.tools = [{"name": tool_name, "description": tool_description, "input_schema": schema}]

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def tool_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_err} for tid, content, is_err in results]})

    def step(self) -> Step:
        kwargs: dict[str, Any] = {}
        if not self.model.startswith("claude-haiku"):
            kwargs["thinking"] = {"type": "adaptive"}
        resp = self.client.messages.create(model=self.model, max_tokens=4096, system=self.system,
                                           tools=self.tools, messages=self.messages, **kwargs)
        self.messages.append({"role": "assistant", "content": resp.content})
        text = "\n".join(b.text for b in resp.content if b.type == "text") or None
        calls = [ToolCall(b.id, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return Step(text, calls)


class OpenAIAdapter:
    """Any OpenAI-compatible chat endpoint (OpenAI, Ollama, vLLM, LM Studio…)."""

    def __init__(self, model: str, base_url: str | None, *, api_key: str | None = None,
                 default_headers: dict[str, str] | None = None):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key or os.environ.get("OPENAI_API_KEY", "none"),
                             default_headers=default_headers)
        self.model = model
        self.messages: list[dict] = []

    def start(self, system, tool_name, tool_description, schema):
        self.messages = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": {"name": tool_name, "description": tool_description, "parameters": schema}}]

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def tool_results(self, results):
        for tid, content, _ in results:
            self.messages.append({"role": "tool", "tool_call_id": tid, "content": content})

    def step(self) -> Step:
        resp = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools)
        msg = resp.choices[0].message
        self.messages.append(msg.model_dump(exclude_none=True))
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {"line": tc.function.arguments}
            calls.append(ToolCall(tc.id, args if isinstance(args, dict) else {"line": str(args)}))
        return Step(msg.content or None, calls)


class OpenRouterAdapter(OpenAIAdapter):
    """OpenRouter via its OpenAI-compatible chat-completions endpoint."""

    def __init__(self, model: str):
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for --provider openrouter")
        super().__init__(
            model,
            "https://openrouter.ai/api/v1",
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/herclogon/aci",
                "X-OpenRouter-Title": "AAI evaluation harness",
            },
        )


class ScriptedAdapter:
    """Deterministic agent for testing the harness itself. Script items: {"line": ...} | {"say": ...}."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.n = 0

    def start(self, *a): pass
    def user(self, text): pass
    def tool_results(self, results): pass

    def step(self) -> Step:
        if not self.script:
            return Step("done", [])
        item = self.script.pop(0)
        if "say" in item:
            return Step(item["say"], [])
        self.n += 1
        args = {"line": item["line"]}
        if "input" in item:
            args["input"] = item["input"]
        return Step(None, [ToolCall(f"s{self.n}", args)])


# ---------------------------------------------------------------- running

@dataclass
class CaseResult:
    name: str
    passed: bool
    calls: int
    checks: dict[str, bool]
    violations: dict[str, int]
    final_text: str
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None


def state_check(server: MockServer, name: str, value: Any = None) -> bool:
    if name == "simulation_named":
        return any(s["name"] == value for s in server.simulations.values())
    if name == "workflow_deleted":
        return value not in server.workflows
    if name == "run_succeeded":
        return any(r["state"] == "succeeded" for r in server.runs.values())
    if name == "no_runs":
        return not server.runs
    raise ValueError(f"unknown state check {name}")


def run_case(case: dict, adapter: Adapter, server: MockServer, fmt: str, verbose: bool) -> CaseResult:
    server.reset()
    system = load_system_prompt(server)
    adapter.start(system, server.manifest()["name"], bootstrap_text(server), tool_schema())
    turns = list(case["turns"])
    confirm_turn = case.get("confirm_turn")
    max_calls = case.get("max_calls", 12)
    transcript: list[dict] = []
    violations = {k: 0 for k in ("yes_before_confirm", "placeholder_sent", "shell_syntax_rejected", "unknown_command", "invalid_args", "repeated_unchanged_line")}
    calls = 0
    turn_idx = 0
    final_text = ""
    last_line: str | None = None
    last_ok = True
    error = None

    adapter.user(turns[0]); transcript.append({"user": turns[0]})
    if verbose: print(f"  user: {turns[0]}")
    try:
        while True:
            step = adapter.step()
            if step.text:
                transcript.append({"assistant": step.text})
                if verbose: print(f"  assistant: {step.text[:300]}")
            if not step.tool_calls:
                final_text = step.text or ""
                turn_idx += 1
                if turn_idx < len(turns):
                    adapter.user(turns[turn_idx]); transcript.append({"user": turns[turn_idx]})
                    if verbose: print(f"  user: {turns[turn_idx]}")
                    continue
                break
            results = []
            for tc in step.tool_calls:
                calls += 1
                req: dict = {"aai": "0.2"}
                if "line" in tc.args and tc.args["line"] is not None:
                    req["line"] = str(tc.args["line"])
                elif "argv" in tc.args:
                    req["argv"] = list(tc.args["argv"])
                else:
                    req["line"] = ""
                if tc.args.get("input") is not None:
                    req["input"] = tc.args["input"]
                if fmt == "text":
                    req["options"] = {"format": "text"}
                line_repr = req.get("line") or " ".join(req.get("argv", []))
                # violations observable from the request itself
                if re.search(r"<[a-z_]+>", line_repr):
                    violations["placeholder_sent"] += 1
                if "--yes" in line_repr.split() and (confirm_turn is not None and turn_idx < confirm_turn):
                    violations["yes_before_confirm"] += 1
                if line_repr == last_line and not last_ok:   # retrying a line that just failed, unchanged
                    violations["repeated_unchanged_line"] += 1
                last_line = line_repr
                resp = server.invoke(req)
                last_ok = bool(resp["ok"])
                if not resp["ok"] and resp["error"]["code"] in violations:
                    violations[resp["error"]["code"]] += 1
                content = render_text(resp) if fmt == "text" else json.dumps(resp, ensure_ascii=False)
                transcript.append({"call": req, "response": resp})
                if verbose:
                    print(f"  > {line_repr}" + (f"   input={json.dumps(req['input'])}" if 'input' in req else ""))
                    print("    " + content.replace("\n", "\n    ")[:600])
                results.append((tc.id, content, not resp["ok"]))
            adapter.tool_results(results)
            if calls >= max_calls:
                error = f"call budget {max_calls} exhausted"
                break
    except Exception as e:  # provider errors should not kill the whole run
        error = f"{type(e).__name__}: {e}"

    checks = {}
    for c in case["checks"]:
        key = c["type"] + ":" + str(c.get("path") or c.get("name") or c.get("values"))
        checks[key] = evaluate_check(c, server, final_text)
    passed = all(checks.values()) and not any(violations[k] for k in HARD_VIOLATIONS) and error is None
    return CaseResult(case["name"], passed, calls, checks, violations, final_text, transcript, error)


def evaluate_check(c: dict, server: MockServer, final_text: str) -> bool:
    t = c["type"]
    if t == "command_ok":
        return any(e["response"]["ok"] and e["response"]["command"] == c["path"]
                   and (not c.get("dry_run") or "--dry-run" in (e["request"].get("line") or " ".join(e["request"].get("argv", []))))
                   for e in server.log)
    if t == "command_error":
        return any(not e["response"]["ok"] and e["response"]["command"] == c["path"] and e["response"]["error"]["code"] == c["code"] for e in server.log)
    if t == "state":
        return state_check(server, c["name"], c.get("value"))
    if t == "final_text_contains_any":
        return any(v.lower() in final_text.lower() for v in c["values"])
    if t == "final_text_contains_all":
        return all(v.lower() in final_text.lower() for v in c["values"])
    raise ValueError(f"unknown check {t}")


def make_adapter(args: argparse.Namespace, case: dict) -> Adapter:
    if args.provider == "anthropic":
        return AnthropicAdapter(args.model or "claude-opus-5")
    if args.provider == "openai":
        if not args.model:
            sys.exit("--model is required for --provider openai")
        return OpenAIAdapter(args.model, args.base_url)
    if args.provider == "openrouter":
        if not args.model:
            sys.exit("--model is required for --provider openrouter")
        return OpenRouterAdapter(args.model)
    if args.provider == "scripted":
        from scripted_agents import SCRIPTS
        return ScriptedAdapter(SCRIPTS[case["name"]])
    sys.exit(f"unknown provider {args.provider}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["anthropic", "openai", "openrouter", "scripted"], required=True)
    ap.add_argument("--model")
    ap.add_argument("--base-url", help="OpenAI-compatible endpoint (Ollama: http://localhost:11434/v1)")
    ap.add_argument("--format", choices=["text", "json"], default="text", help="How results are shown to the model")
    ap.add_argument("--case", action="append", help="Run only these case names")
    ap.add_argument("--repeat", type=int, default=1, help="Runs per case (models are stochastic)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        ap.error("OPENROUTER_API_KEY is required for --provider openrouter")

    cases = json.loads((Path(__file__).parent / "cases.json").read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["name"] in args.case]
    server = MockServer()
    results: list[CaseResult] = []
    label = args.model or args.provider
    print(f"model={label} provider={args.provider} format={args.format}\n")
    for case in cases:
        for i in range(args.repeat):
            print(f"[{case['name']}]" + (f" run {i+1}/{args.repeat}" if args.repeat > 1 else ""))
            r = run_case(case, make_adapter(args, case), server, args.format, args.verbose)
            results.append(r)
            viol = ", ".join(f"{k}={v}" for k, v in r.violations.items() if v)
            failed = [k for k, ok in r.checks.items() if not ok]
            print(f"  {'PASS' if r.passed else 'FAIL'}  calls={r.calls}" + (f"  violations: {viol}" if viol else "")
                  + (f"  failed: {failed}" if failed else "") + (f"  error: {r.error}" if r.error else ""))
    n_pass = sum(r.passed for r in results)
    print(f"\n{n_pass}/{len(results)} passed; avg calls {sum(r.calls for r in results)/max(len(results),1):.1f}")

    out_dir = Path(__file__).parent / "results"; out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{re.sub(r'[^a-z0-9.-]+', '_', label.lower())}-{args.format}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"model": label, "provider": args.provider, "format": args.format,
                               "passed": n_pass, "total": len(results),
                               "cases": [r.__dict__ for r in results]}, indent=2, ensure_ascii=False, default=str))
    print(f"report: {out}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
