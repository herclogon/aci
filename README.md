# ACI — Agent Command Interface

[![Tests](https://github.com/herclogon/aci/actions/workflows/tests.yml/badge.svg)](https://github.com/herclogon/aci/actions/workflows/tests.yml)

A protocol for exposing an application to AI agents the way a good
command-line tool exposes itself to a person: **one entry point**, a
**command tree discovered at runtime**, commands organised around **what
users want to do**, and **built-in guidance** (help, examples, scenarios,
actionable errors) delivered in small pieces on demand.

```
invoke(argv) -> result
```

That is the whole protocol. Listing commands, reading a command's contract,
searching by intent, following a scenario, tracking long-running work — all
of it is itself a command in the tree. An agent needs a ~130-token
bootstrap text to use any ACI application, whether it has 10 commands or
10,000.

**Status:** Draft 0.2 — specification only, written to be implementable.
No reference implementation yet; the repo ships a mock server used to
evaluate how well different LLMs cope with the protocol.

## Why

Native tool calling and MCP register every tool up front, and every tool
costs context on every turn. Dozens of tools cost 10–20K tokens before any
work begins, and they carry no hierarchy, no `--help`, no procedures.
Agents are demonstrably productive with `git`, `gh`, `kubectl` — tools
whose interface is discovered progressively and organised by task.

ACI is that interface with the shell removed and the output made
structural:

| | Tool calling / MCP | ACI |
|---|---|---|
| Bootstrap cost | grows with the tool count | constant |
| Discovery | eager dump | `help` → `help <group>` → `<command> --help` |
| Organising principle | operations / resources | user scenarios |
| Arguments | nested JSON | one command line (`line`), `@input` for structured values |
| Guidance | schema only | examples, scenarios, `hint` + `fix` on every error |
| Composition | client knows foreign keys | typed handles flow from output to input |

It is exposed to a model as **one tool** (native tool-use API or MCP), or
served over HTTP, JSON-RPC, or stdio. Not a shell: `line` is lexed, never
executed; only registered commands run.

## What it looks like

```
> explore "optimize wing geometry using my existing CFD model"
  Scenario optimize-geometry — steps:
    1 simulation inspect <model> --parameters
    2 optimization configure --model <model> --vars @input.vars --objectives @input.objectives
    3 workflow validate <workflow>
    4 workflow run --workflow <workflow>        (async)
    5 task wait <task>
    6 results pareto <run> --explain

> simulation inspect wing3
  error not_found: No simulation with id or name 'wing3'.
  hint: Run `simulation list` to see available models.
  did you mean: simulation inspect wing_v3 | simulation inspect sim_wing3

> workflow run --workflow wf_opt11
  ok: Started run r_42 of workflow wf_opt11 (est. 4 min).
  handles: r_42 (run)   task: t_918 queued   next: task wait t_918

> workflow delete wf_opt11
  error confirmation_required: Deleting wf_opt11 removes 1 run (r_42) and its results.
  fix: workflow delete wf_opt11 --yes
```

## Design principles

1. Constant-size bootstrap — the agent's prior knowledge does not grow with the app.
2. Pay only for what you read — one line per child in listings; full contracts per command.
3. Organise around intent — commands are steps of scenarios, not CRUD over tables.
4. Every response teaches — `summary`, `next`, `hint`, `fix`.
5. Text-shaped but typed — argv tokens, validated against declared schemas.
6. Not a shell.
7. Summaries first, data on request — pagination and handles keep responses small.
8. Transport-independent.
9. Guidance never grants — only execution enforces permissions.
10. Model-agnostic — usable by a model that can only emit one string and read plain text.

## Repository

| Path | What |
|---|---|
| [`SPEC.md`](SPEC.md) | The protocol: envelope, argv grammar, descriptors, scenarios, tasks, handles, sessions, permissions, error catalogue, transport bindings, JSON schemas, conformance checklist. |
| [`AGENT_PROMPT.md`](AGENT_PROMPT.md) | A ~230-token system prompt a host puts in front of an agent to drive any ACI application. |
| [`tests/`](tests/) | LLM evaluation harness: an in-process mock ACI application, eval cases, and a runner for Anthropic / OpenAI-compatible endpoints (including local models via Ollama). See [`tests/README.md`](tests/README.md). |
| [`CHANGELOG.md`](CHANGELOG.md) | Changes between drafts. |

## Conformance levels

| Level | Reserved commands | Required |
|---|---|---|
| Core | `info`, `help`, `<path> --help` | MUST |
| Search | `search` | SHOULD |
| Scenarios | `explore`, `scenario list\|show` | SHOULD |
| Tasks | `task status\|wait\|cancel\|logs\|list` | MUST if any command is async |
| Sessions | `session show\|set\|unset\|reset` | MAY |
| Handles | `handle show\|read\|release` | MUST if large outputs are returned |
| Streaming | transport-level task events | MAY |

A server declares its levels in the manifest returned by `info`. The full
checklist is §10 of the spec.

## Evaluating a model

```sh
python -m unittest tests/test_harness.py                       # no keys needed
python tests/llm_eval.py --provider scripted                  # golden agent scripts
python tests/llm_eval.py --provider anthropic --model claude-opus-5
python tests/llm_eval.py --provider openrouter --model qwen/qwen3-32b
python tests/qwen_openrouter.py                               # Qwen size sweep
python tests/llm_eval.py --provider openai --model llama3.1:8b --base-url http://localhost:11434/v1
```

Each case gives the model a user goal and scores whether it reached the
right commands, recovered from errors using `hint`/`fix`/`did_you_mean`,
quoted values correctly, used `@input` for structured data, followed an
async task to completion, and — the hard rule — never sent `--yes` before
the user confirmed. The point is to measure the protocol's guidance, not
the model: if a small model fails a case, the fix is usually in the
prompt, the help text budget, or the error hints.

## Relationship to other protocols

- **Native tool calling / MCP** — ACI is exposed as one tool; its tree replaces a growing tool list.
- **OpenAPI / GraphQL** — typical backends behind an ACI server; descriptors may reference their schemas.
- **Agent Skills** — an ACI scenario is the executable counterpart of a skill's procedure.
- **A2A / ACP** — orthogonal; an agent card can advertise an ACI endpoint.
- **Classic CLI** — a CLI can be generated from any ACI server, and is the recommended way to test a tree.

## Contributing

The spec is open for revision. Issues and pull requests against `SPEC.md`
are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md). Open design
questions are listed in §12 of the spec.

## License

[MIT](LICENSE).
