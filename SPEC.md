# Agent Command Interface (ACI)

**Protocol specification — Draft 0.2**

Status: draft, open for revision. Language-agnostic, transport-agnostic.
Intended to be implementable: two independent implementations of this
document should interoperate. Items in §12 are explicitly outside
conformance.

---

## 0. Summary

ACI is a protocol for exposing an application to AI agents the way a good
command-line tool exposes itself to a person: through a **single entry point**,
a **hierarchical command tree that is discovered at runtime**, commands that
are **organized around what users want to accomplish** rather than around the
application's data model, and **built-in guidance** (help, examples, scenarios,
actionable errors) delivered in small, on-demand pieces.

The whole protocol reduces to one operation:

```
invoke(argv) -> result
```

Everything else — listing commands, reading a command's contract, searching by
intent, following a scenario, tracking long-running work — is itself a command
in the tree. An agent needs to know a ~130-token bootstrap text to use any
ACI application, regardless of whether that application has 10 or 10,000
commands.

ACI is **not** a shell. `argv` is parsed against declared schemas; there is
no process execution, no pipes, no expansion.

---

## 1. Motivation

### 1.1 Why CLIs work well for agents

Agents (and people) are productive with CLIs such as `git`, `gh`, `kubectl`
because those tools have properties that conventional REST/GraphQL APIs lack:

| Property | CLI | Typical REST / OpenAPI |
|---|---|---|
| Bootstrap cost | Constant: "run `tool --help`" | Whole spec, or a curated subset, must be in context |
| Discovery | Hierarchical, progressive, on demand | Flat endpoint list; discovery is a one-shot dump |
| Organizing principle | User tasks (`pr create`, `rollout restart`) | Resources and their fields |
| Argument syntax | Compact, text-native, model already fluent | Nested JSON bodies to construct |
| Guidance | `--help`, examples, "did you mean" | Schema only; procedure must be reconstructed |
| Errors | Human-readable with hints | Status codes and opaque payloads |
| Composition | Output ids feed the next command | Client must know foreign-key relationships |

### 1.2 Why existing agent protocols don't cover this

- **Native tool calling** (provider tool-use / function-calling APIs) is the
  mechanism by which an LLM invokes anything, but it is *eager*: every tool
  definition is registered up front and costs context on every turn. An
  application with 200 operations either registers 200 tools or hand-curates
  a subset. Tool calling has no hierarchy, no help, no scenarios.
- **MCP** standardizes tool discovery and invocation across hosts, but
  inherits the same eager model: tool definitions are loaded up front and
  each costs context. Dozens of tools cost 10–20K tokens before any work
  begins. MCP has no notion of a command hierarchy, of scenarios, or of
  `--help`-style progressive disclosure.
- **OpenAPI / GraphQL** describe data and endpoints, not how to get things
  done.
- **Agent Skills** provide progressive procedural disclosure but are
  instructions, not an executable interface.
- **A2A / ACP (Agent Client Protocol)** manage agent-to-agent delegation and
  editor↔agent sessions; they do not describe application capabilities.

ACI fills the gap: a **searchable, executable, self-describing command tree**.
It is exposed to an LLM as **one tool** whose description is a constant
bootstrap text (§5.4), and can equally be served over HTTP, JSON-RPC or stdio
— the transport does not determine the context cost.

---

## 2. Design principles

| # | Principle | Consequence |
|---|---|---|
| P1 | **Constant-size bootstrap** | The agent's prior knowledge is a fixed text, independent of app size. |
| P2 | **Pay only for what you read** | Tree listings are one line per child; full contracts are fetched per command. |
| P3 | **Organize around intent** | Commands are steps of user scenarios, not CRUD over tables. |
| P4 | **Every response teaches** | Results carry `summary`, `next` suggestions, and errors carry `hint`/`fix`. |
| P5 | **Text-shaped, but typed** | Arguments are argv tokens; the server validates them against a declared schema. |
| P6 | **Not a shell** | No process execution, pipes, globbing, variables. Only registered commands run. |
| P7 | **Summaries first, data on request** | Default output is short; large data is paginated or returned as a handle. |
| P8 | **Transport-independent** | One request/response envelope; bindings for LLM tool APIs, MCP, HTTP, JSON-RPC, stdio. |
| P9 | **Guidance never grants** | Help/scenarios describe permissions; only execution enforces them. |
| P10 | **Model-agnostic** | Usable by a model that can only reliably emit one string (`line`) and read plain text. Nothing an agent must do requires constructing or parsing nested JSON. |

---

## 3. Terminology

| Term | Meaning |
|---|---|
| **Application** | The system exposed through ACI (one command tree, one manifest). |
| **Server** | The implementation that receives `invoke` requests and serves the tree. |
| **Client / Agent** | The caller. Usually an LLM agent; may be a human via a generated CLI. |
| **Caller identity** | The authenticated principal behind a request, as established by the transport. Tasks, temporary handles, sessions and idempotency keys are scoped to it. |
| **Command tree** | A rooted tree. Internal nodes are **groups**, leaves are **commands**. |
| **Path** | A node's position: `["workflow", "run"]`, rendered `workflow run` or `workflow.run`. |
| **argv** | Array of string tokens: path followed by positional args and flags. |
| **Descriptor** | The machine-readable contract of a command (what `--help` returns). |
| **Scenario** | A named, multi-step procedure that accomplishes a user goal using commands. |
| **Task** | A long-running execution with observable state. |
| **Handle** | An opaque, typed identifier returned by a command and accepted by others. |
| **Session** | Optional server-side context (defaults, scope) shared across invocations. |

**Tree rules.** A node is exactly one of `group` or `command`; a command
MUST NOT have children. Node names MUST match `^[a-z][a-z0-9-]*$`. The
reserved root names (§4.2) MUST NOT be used at the root and SHOULD NOT be
used deeper in the tree.

Key words MUST, SHOULD, MAY are used as in RFC 2119.

---

## 4. Protocol model

### 4.1 The single operation: `invoke`

**Request**

```json
{
  "aci": "0.2",
  "id": "req-1",
  "argv": ["workflow", "run", "--workflow", "wing", "--params", "@input.params"],
  "input": { "params": { "mach": 0.8, "alpha": [0, 2, 4] } },
  "session": "s_7f3a",
  "options": {
    "format": "summary",
    "limit": 20,
    "timeout_ms": 5000,
    "idempotency_key": "b1e2…"
  }
}
```

| Field | Req. | Description |
|---|---|---|
| `aci` | yes | Protocol version, `MAJOR.MINOR`. See *Versioning* below. |
| `id` | no | Opaque correlation id, echoed verbatim in the response. Needed on pipelined transports (stdio). |
| `line` | yes* | The command as one string, e.g. `"workflow run --workflow wing"`, lexed by the server per §4.3.1. The primary form (P10). |
| `argv` | yes* | Alternative to `line`: pre-split tokens, path first. MUST be non-empty. Exactly one of `argv`/`line` MUST be present. |
| `input` | no | A JSON payload playing the role of stdin. Referenced from argv with `@input` or `@input.<path>` (§4.3.3). |
| `session` | no | Session id (§4.9). |
| `options` | no | Cross-cutting options; each has a global-flag equivalent (§4.3.4). |

Unknown top-level fields and unknown `options` keys MUST be ignored.

**Versioning.** A server MUST accept a request whose `aci` MAJOR equals its
own and whose MINOR is ≤ its own; the response `aci` is always the server's
version. Any other version → `unsupported_version` with
`details.supported`.

**Malformed envelope** (not JSON, neither or both of `argv`/`line`, empty
`argv`, wrong field types) → `bad_request`. The server MUST still answer
with an error envelope.

**Response (success)**

```json
{
  "aci": "0.2",
  "id": "req-1",
  "ok": true,
  "command": "workflow.run",
  "summary": "Started run r_42 of workflow 'wing' (12 design points, est. 4 min).",
  "data": { "run": "r_42", "points": 12, "estimate_s": 240 },
  "handles": [ { "id": "r_42", "type": "run", "label": "wing / 2026-09-21 11:52", "expires": null } ],
  "task": { "id": "t_918", "state": "running" },
  "next": [
    { "argv": ["task", "wait", "t_918"], "why": "Block until the run completes." },
    { "argv": ["workflow", "results", "r_42", "--best"], "why": "Inspect results once done." }
  ],
  "warnings": [],
  "truncated": null
}
```

| Field | Req. | Description |
|---|---|---|
| `id` | echo | Present iff the request carried one. |
| `ok` | yes | `true` on success. |
| `command` | yes | Dotted path of the **deepest resolved node**: the command, the group resolution stopped on, or `""` if nothing resolved. Never null. |
| `summary` | yes | 1–3 sentences. `summary`, `next` and (on errors) `hint`/`fix` MUST make the result actionable without reading `data`. |
| `data` | no | Structured result. Shaped by `--format`, `--fields`, `--limit`. |
| `text` | no | Human-readable rendering (tables etc.) when `--format text`. |
| `handles` | no | Typed ids produced by this command (§4.10). |
| `task` | no | Present when execution continues asynchronously (§4.8). |
| `next` | no | Suggested follow-up invocations. Advisory. Each `argv` MUST be runnable as-is (no placeholders). |
| `warnings` | no | Non-fatal notes (deprecations, clamped limits, partial results). |
| `truncated` | no | Map of `data` paths that were cut, with `shown`, `total`, `cursor` (§4.11). |

**Response (error)**

```json
{
  "aci": "0.2",
  "ok": false,
  "command": "workflow.run",
  "error": {
    "code": "missing_arg",
    "message": "Required flag --workflow is missing.",
    "hint": "Pass the workflow name or handle. List workflows with `workflow list`.",
    "fix": [ ["workflow", "run", "--workflow", "<name>"] ],
    "did_you_mean": null,
    "help": ["workflow", "run", "--help"],
    "retryable": false,
    "details": { "missing": ["workflow"] }
  }
}
```

`fix` entries are templates and MAY contain `<placeholder>` tokens; `next`
entries never do. Error codes and their precedence are in §4.14.

### 4.2 Reserved commands and conformance levels

Certain names are reserved at the tree root. A server declares which levels
it supports in its manifest (`info`, schema A.9).

| Level | Reserved names | Required? |
|---|---|---|
| **Core** | `info`, `help`, `<path> --help` | MUST |
| **Search** | `search` | SHOULD |
| **Scenarios** | `explore`, `scenario list`, `scenario show` | SHOULD |
| **Tasks** | `task list|status|wait|cancel|logs` | MUST if any command is `async` |
| **Sessions** | `session show|set|unset|reset` | MAY |
| **Handles** | `handle show|read|release` | MUST if any command returns `blob`/`table` handles |
| **Streaming** | transport-level task events | MAY |

`task`, `scenario`, `session` and `handle` are groups; the rest are
commands. Applications MUST NOT define their own root nodes with these names.
When a level is not supported, its reserved names resolve as
`unknown_command`.

#### `info`
Returns the manifest (A.9):

```json
{
  "name": "engineering",
  "title": "Engineering Platform",
  "version": "3.2.0",
  "aci": "0.2",
  "levels": ["core", "search", "scenarios", "tasks", "sessions", "handles"],
  "description": "Build, validate and run simulation and optimization workflows.",
  "top_scenarios": ["optimize-geometry", "import-cfd-model", "compare-runs"],
  "bootstrap": "You can use the application \"engineering\" by invoking …",
  "auth": { "scheme": "bearer", "obtain": "https://…/tokens" },
  "session_keys": [ { "name": "project", "help": "Default project scope." } ],
  "limits": { "max_help_depth": 3, "max_limit": 500, "task_wait_max_ms": 60000 }
}
```

#### `help [PATH...] [--depth N]`
Lists **one level** of the tree at `PATH` (root if omitted). One line per
child: name, kind, summary. Groups are marked; commands the caller may not
run are marked `locked` (§4.13).

```
engineering — Build, validate and run simulation and optimization workflows.

Groups:
  simulation    Import and inspect simulation models
  workflow      Create, validate, run and inspect workflows
  optimization  Configure optimization studies
  results       Query and compare run results
  task          Track long-running operations
  scenario      List and show step-by-step scenarios
  session       Manage per-caller defaults
  handle        Inspect and read handles

Commands:
  info          Show the application manifest
  search        Find commands and scenarios by intent
  explore       Get a step-by-step scenario for a goal

Scenarios (top): optimize-geometry, import-cfd-model, compare-runs
Use `help <group>` to go deeper, `<command> --help` for a contract.
```

- `--depth` MAY expand nested levels; a value above `limits.max_help_depth`
  is clamped and noted in `warnings`.
- `help <command-path>` is equivalent to `<command-path> --help`, and
  `<group-path> --help` is equivalent to `help <group-path>`.
- **Invoking a bare group path** (e.g. `["workflow"]`) MUST return the same
  response as `help workflow`, with `ok: true` and `command: "workflow"`.
- A group path followed by a token that names no child
  (`["workflow", "wing"]`) → `unknown_command`; `did_you_mean` is drawn
  from that group's children and `help` is `["help", "workflow"]`.

#### `<PATH...> --help`
Returns the full **CommandDescriptor** (§4.4) for a command, or the
**TreeNode** (§4.5) for a group. This is the only place where argument
schemas, examples, side effects and permissions are exposed in full.

#### Output format of discovery commands
For `info`, `help`, `--help`, `scenario list`, `scenario show` and
`explore`, `--format summary` is treated as `text`. With `text`, only the
`text` field is populated (rendering per Appendix B); with `json`, only
`data` is (Manifest / TreeNode / CommandDescriptor / Scenario). Never both.
`summary` is still present as one line (e.g. "workflow: 1 group, 4
commands").

#### `search QUERY [--kind command|scenario|all] [--limit N]`
Ranks commands and scenarios by intent match (§4.7).

#### `explore GOAL` / `scenario list` / `scenario show NAME`
See §4.6.

#### `task …`, `session …`, `handle …`
See §4.8, §4.9, §4.10.

### 4.3 argv grammar

```
argv        := path+ (positional | flag)*
path        := token                         ; resolved greedily against the tree
positional  := token
flag        := "--" name "=" value            ; explicit value
             | "--" name [ value ]            ; next token is the value (non-bool)
             | "-" letter [ value ]           ; short alias: -l 20 or -l20
             | "--no-" name                   ; boolean negation
             | "--"                           ; end of flags; rest are positionals
value       := token | "@input" | "@input." jsonpath
```

**Path resolution.** The longest prefix of `argv` that names a node wins;
remaining tokens are arguments. Because a command has no children,
resolution stops at the first command reached.

**Flags and positionals.**

1. `bool` flags never consume the next token: `--verbose foo` sets
   `verbose=true` and `foo` is a positional. An explicit value is given only
   as `--verbose=false`. `--no-<name>` is valid only for `bool` flags; on
   any other type → `invalid_args`.
2. Non-`bool` flags take exactly one value, from `--name=value` or the next
   token — even when that token begins with `--`. A missing value →
   `missing_arg` with `details.missing: ["<name>"]`.
3. Short aliases: `-l 20` and `-l20` are both valid; bundling (`-abc`) →
   `invalid_args`. `short` MUST be a single letter, unique within the
   command, and MUST NOT be `h`.
4. A token matching `^-[0-9]` is a value, never a flag.
5. Positionals and flags MAY interleave; `--` ends flag parsing.
6. `args` are matched in order; only the last MAY be `repeatable`. Too many
   positionals → `invalid_args`; missing required → `missing_arg`.
7. Repeating a `repeatable` flag appends; repeating a scalar flag →
   `invalid_args`.
8. Unknown flag → `invalid_args` with `did_you_mean` holding the closest
   declared flag names (as argv fragments, e.g. `[["--workflow"]]`).
9. Values are strings on the wire; the server coerces them per §4.4.1. A
   value that does not coerce → `invalid_value`.

#### 4.3.1 `line` lexing

A server MUST lex `line` as follows and MUST NOT perform any expansion:

- Unquoted whitespace separates tokens.
- Single quotes: every character literal until the closing quote.
- Double quotes: literal except `\"` and `\\`.
- Backslash outside quotes escapes the next character.
- Unterminated quote → `invalid_args`.
- Tolerance (weak models echo their prompt): a leading `$` or `>`
  followed by whitespace is stripped from the raw string *before* the
  operator check below; after lexing, a first token equal to the manifest
  `name` is dropped; a trailing newline is ignored. Nothing else is
  auto-corrected — `did_you_mean` handles the rest.

After lexing, if any of `| & ; < > ( ) $ ` * ? [ ~` occurred **unquoted and
unescaped**, the request → `shell_syntax_rejected`. Inside quotes or
escaped, these characters are ordinary data (`search "price > 5"` is
valid). The `hint` MUST mention both alternatives — quote the value or pass
it via `@input` — and MUST note that `<name>` is a placeholder to be
substituted, since agents copy scenario steps verbatim.

#### 4.3.2 Global flags

Recognized on every command, reserved. A descriptor MUST NOT declare a flag
with one of these names; a server MUST reject such a tree at load time.
Global flags have no short aliases.

| Flag | Meaning |
|---|---|
| `--help` | Return the descriptor instead of executing. |
| `--format summary|json|text` | Output shaping (§4.11). Default `summary`. |
| `--fields a,b.c` | Project `data` to the listed paths. |
| `--limit N` / `--cursor C` | Pagination of list-like `data`. |
| `--dry-run` | Validate and describe the effect without performing it (§4.12). |
| `--yes` | Confirm a destructive or billing action (§4.12). |
| `--async` | Prefer task-style execution when the command supports it (§4.8). |
| `--quiet` | Omit `next`, `warnings`, `text`. |

`--fields`, `--limit` and `--cursor` on a command whose `data` is neither an
object nor a list are ignored with a `warnings` entry.

#### 4.3.3 `@input` substitution

- Applies to positional and flag values only. `@input…` in a path position
  is `unknown_command`.
- `@input` substitutes the whole `input` object; `@input.<path>` a member.
  `<path>` is dotted keys with optional `[n]` indexing (`params.alpha[1]`).
  Keys containing `.` or `[` are unreachable this way — pass `@input` into
  a `json` flag instead.
- The substituted value is JSON, not a string, and is coerced by §4.4.1:
  `json` accepts anything; `string` accepts a JSON string only; `int`,
  `number`, `bool` accept the matching JSON type or a string that coerces;
  `enum` a string in `values`; `handle:<t>` a string id or a Handle object
  (its `id` is used). An object or array into a scalar type →
  `invalid_value` with `details.expected`.
- A JSON array substituted into a `repeatable` param populates it entirely
  (as if the flag were repeated per element); a scalar appends one element.
- An unresolved reference (`input` absent or path missing) →
  `invalid_value` with `details.flag` and `details.ref` (e.g.
  `"input.params"`); the hint tells the agent to add it to `input`.
- A token beginning with `@@` is passed literally with one `@` removed —
  the only way to send a literal string starting with `@input`. Any other
  token beginning with `@` is passed literally.

#### 4.3.4 `options` ↔ global flags

| `options` key | Flag | Notes |
|---|---|---|
| `format` | `--format` | |
| `fields` | `--fields` | |
| `limit`, `cursor` | `--limit`, `--cursor` | |
| `dry_run` | `--dry-run` | |
| `confirm` | `--yes` | |
| `async` | `--async` | |
| `quiet` | `--quiet` | |
| `timeout_ms` | — | options only |
| `idempotency_key` | — | options only |

When both are given, the flag wins.

### 4.4 CommandDescriptor

Returned by `<path> --help --format json`. Compact JSON Schema in Appendix A.4.

```json
{
  "path": ["workflow", "run"],
  "summary": "Execute a workflow and start an observable run.",
  "description": "Runs a validated workflow with the given parameters. Long runs continue as a task.",
  "usage": "workflow run --workflow <name|handle> [--params @input.params] [--priority low|normal|high]",
  "args": [],
  "flags": [
    { "name": "workflow", "type": "handle:workflow|string", "required": true,
      "help": "Workflow name or handle.", "from_session": "workflow" },
    { "name": "params", "type": "json", "required": false,
      "help": "Parameter overrides. Usually passed as @input.params." },
    { "name": "priority", "type": "enum", "values": ["low", "normal", "high"], "default": "normal" }
  ],
  "input": { "schema_ref": "#/schemas/RunParams", "help": "Optional parameter overrides." },
  "output": {
    "summary_of": "run id, point count, estimated duration",
    "handles": ["run"],
    "schema_ref": "#/schemas/RunStarted"
  },
  "examples": [
    { "argv": ["workflow", "run", "--workflow", "wing"], "comment": "Run with stored parameters." },
    { "argv": ["workflow", "run", "--workflow", "wing", "--params", "@input.params"],
      "input": { "params": { "mach": 0.8 } }, "comment": "Override parameters." }
  ],
  "effects": ["write", "external"],
  "async": true,
  "permissions": ["workflow.read", "execution.run"],
  "preconditions": [
    { "text": "Workflow must pass validation.", "check": ["workflow", "validate", "<workflow>"] }
  ],
  "errors": [
    { "code": "precondition_failed", "when": "Workflow has validation errors.",
      "fix": ["workflow", "validate", "<workflow>", "--explain"] }
  ],
  "related": [["workflow", "validate"], ["workflow", "results"], ["task", "wait"]],
  "scenarios": ["optimize-geometry"],
  "stability": "stable",
  "since": "3.0"
}
```

Field notes:

- `summary` MUST be ≤ 80 characters — it is what appears in tree listings.
- `effects` values: `write`, `destructive`, `external`, `billing`. The
  field is required; an **empty array means the command is a pure read**.
- `async: true` means the command MAY continue as a task (§4.8); absent or
  false means it never does.
- `examples` are REQUIRED (≥ 1). Models learn faster from examples than from
  schemas.
- `preconditions[].check` is an argv the agent can run to verify readiness.
- `stability: deprecated` — invoking succeeds with a `warnings` entry naming
  `replaced_by` (a path) when present. `experimental` adds no behaviour.
- A param with `required: true` and a `default` is an invalid tree. `values`
  is permitted only when `type` includes `enum`. A `repeatable` param is
  typed by its element type.
- `related`, `scenarios`, `next` and `did_you_mean` are filtered by the
  caller's visibility (§4.13).

#### 4.4.1 Parameter types and coercion

| Type | Accepts (argv token) | Notes |
|---|---|---|
| `string` | anything | |
| `int` | `^[+-]?[0-9]+$` | `1e3`, `1.0` → `invalid_value` |
| `number` | JSON number syntax | |
| `bool` | `true`/`false`/`1`/`0`/`yes`/`no`, case-insensitive | only via `--flag=value`; bare flag is `true` |
| `enum` | exact, case-sensitive match in `values` | `did_you_mean` from `values` on miss |
| `json` | token MUST parse as JSON | failure → `invalid_value`, hint suggests `@input` |
| `handle:<t>` | id of an existing handle of type `t` | unknown id → `not_found`; wrong type → `invalid_value` with `details.expected`/`details.got` |
| `duration` | `^([0-9]+(ms\|s\|m\|h\|d))+$`, e.g. `1h30m` | normalized to milliseconds |
| `datetime` | RFC 3339; date-only means 00:00 UTC | |
| `path` | `/`-separated server-side resource path | `..` segments → `invalid_value`; never a client filesystem path |

**Unions** (`a|b`) are tried left to right; the first member that coerces
wins. A `handle:<t>` member whose lookup fails falls through to the next
member instead of raising `not_found` — so `handle:workflow|string` means
"a known handle, else a name".

### 4.5 TreeNode

Returned by `help PATH --format json`:

```json
{
  "path": ["workflow"],
  "kind": "group",
  "summary": "Create, validate, run and inspect workflows",
  "children": [
    { "name": "create",   "kind": "command", "summary": "Create a workflow from a template or model" },
    { "name": "validate", "kind": "command", "summary": "Check that a workflow can execute" },
    { "name": "run",      "kind": "command", "summary": "Execute a workflow and start a run", "effects": ["write"] },
    { "name": "delete",   "kind": "command", "summary": "Delete a workflow", "effects": ["destructive"], "locked": true },
    { "name": "templates","kind": "group",   "summary": "Browse workflow templates" }
  ],
  "scenarios": ["optimize-geometry"]
}
```

Children carry only `name`, `kind`, `summary`, and optional `effects`/`locked`
— never argument schemas.

### 4.6 Scenario

A scenario is procedural knowledge: how to accomplish a goal with the tree.
The server serves scenarios; the **agent** executes them step by step. (A
server-side `scenario run` is a vendor extension, not part of ACI.)

```json
{
  "name": "optimize-geometry",
  "title": "Optimize a geometry using an existing CFD model",
  "intents": ["optimize wing", "run optimization on a CFD model", "improve lift/drag"],
  "summary": "Import a model, define design variables and objectives, validate, run, inspect Pareto front.",
  "when": "You already have a CFD model and want to search over its design parameters.",
  "prerequisites": [
    { "text": "A CFD model is imported.", "check": ["simulation", "list", "--kind", "cfd"] },
    { "text": "You know which parameters vary.", "check": null }
  ],
  "placeholders": [
    { "name": "model",    "type": "handle:simulation",  "from": "user",   "help": "The CFD model to optimize." },
    { "name": "workflow", "type": "handle:workflow",    "from": "step:2", "help": "Generated by optimization configure." },
    { "name": "task",     "type": "handle:task",        "from": "step:4" },
    { "name": "run",      "type": "handle:run",         "from": "step:5" }
  ],
  "steps": [
    { "do": ["simulation", "inspect", "<model>", "--parameters"],
      "expect": "A list of parameters with types and ranges." },
    { "do": ["optimization", "configure", "--model", "<model>", "--vars", "@input.vars", "--objectives", "@input.objectives"],
      "expect": "An optimization handle.",
      "if_fails": "Run `optimization configure --help` and check parameter names against step 1." },
    { "do": ["workflow", "validate", "<workflow>"],
      "expect": "ok=true with no blocking issues." },
    { "do": ["workflow", "run", "--workflow", "<workflow>"],
      "expect": "A task id.", "async": true },
    { "do": ["task", "wait", "<task>"] },
    { "do": ["results", "pareto", "<run>", "--explain"],
      "expect": "Pareto-optimal points with objective values." }
  ],
  "outcomes": ["A run handle with Pareto-optimal designs."],
  "permissions": ["simulation.read", "workflow.create", "execution.run"],
  "commands": [["simulation", "inspect"], ["optimization", "configure"], ["workflow", "validate"],
               ["workflow", "run"], ["results", "pareto"]]
}
```

- `steps[].do`, `prerequisites[].check` and every other argv in the
  protocol are **arrays of tokens**; a string form is not permitted.
- Placeholders in `do` use `<name>`. `placeholders[]` SHOULD declare each
  one with its `type` and origin (`from: "user"` or `"step:N"`, meaning a
  handle returned by that step). Undeclared `<name>` tokens are permitted
  but a conformance SHOULD-violation.
- `explore GOAL` is defined as `search GOAL --kind scenario --limit 3` with
  the top hit expanded: `data.scenarios[0]` is the full Scenario, the rest
  carry `name`, `title`, `summary`, `score` only.
- `scenario list` → `data.scenarios[] { name, title, summary }`, paginated
  like any list. `scenario show NAME` → the Scenario (or `not_found`).

### 4.7 Search

`search QUERY` matches the query against, at minimum: command paths,
summaries, descriptions, example comments, and scenario titles/intents.
Ranking method is implementation-defined (lexical, embedding, hybrid).

```json
{
  "hits": [
    { "kind": "scenario", "name": "optimize-geometry", "summary": "…", "score": 0.91,
      "matched": ["intent: optimize wing"] },
    { "kind": "command", "path": ["optimization", "configure"], "summary": "…", "score": 0.84,
      "matched": ["summary"] }
  ]
}
```

- Each hit has `kind`, `summary`, `score` (higher is better; no cross-server
  meaning), `matched[]`, and `path` (command) or `name` (scenario).
- Servers SHOULD return ≤ 10 hits by default and MUST honour `--limit`.
- Zero hits is `ok: true` with `hits: []` and a `next` pointing at `help`.
- Hits contain summaries only; the agent follows up with `--help`.
- Hidden commands and scenarios (§4.13) never appear.

### 4.8 Tasks

A command declared `async: true` MAY complete asynchronously: when `--async`
is passed, or when execution would exceed `options.timeout_ms` (default
`limits.default_timeout_ms`). In that case the response carries `task`:

```json
"task": { "id": "t_918", "state": "running", "progress": { "percent": 12, "message": "point 2/12" } }
```

States: `queued` → `running` → `succeeded` | `failed` | `cancelled`.

Rules:

- A command with `async: true` that exceeds the timeout MUST convert to a
  task. A command without it MUST return `timeout` instead; the server
  SHOULD abort the work, and if it cannot, MUST say so in `hint` with
  `retryable: false`.
- `--async` on a command without `async: true` is ignored with a `warnings`
  entry.
- The response while a task is `queued`/`running` has `ok: true`; `summary`
  describes what started; `handles` MAY already carry the domain object.
- Tasks are scoped to the caller identity. Another caller's task id →
  `not_found` (never `permission_denied`, to avoid leaking existence).
- Finished tasks MUST remain retrievable for at least
  `limits.task_retention_s`; afterwards → `not_found`.

| Command | Behaviour |
|---|---|
| `task status ID` | `ok: true` with the Task object; when finished, `task.result` holds the final envelope. |
| `task wait ID [--timeout MS]` | Block up to `min(MS, limits.task_wait_max_ms)`, then as `task status`. Returns immediately if already finished. |
| `task cancel ID` | Request cancellation. On `queued` → `cancelled` without running. On a finished task → `conflict`. Effect `destructive` is **not** implied. |
| `task logs ID [--since CURSOR] [--limit N]` | Incremental log lines; `truncated.lines.cursor` is the value for `--since`. |
| `task list [--state S]` | Tasks visible to the caller. |

`task status` and `task wait` return `ok: true` for any existing task,
**including failed ones** — the failure is inside `task.result`, a full
error envelope with the original `error`. The code `task_failed` is used in
that result when the worker failed with no more specific cause.

Streaming (optional): transports MAY push task events (`progress`, `log`,
`done`) — see §5.

### 4.9 Sessions and scope

A session holds caller-specific defaults so repeated arguments can be
omitted — the analogue of `cwd`, `kubectl config use-context`, or
`gh repo set-default`.

```
session set workflow wing        # default for any flag declaring from_session: "workflow"
session set project aero         # key declared in manifest.session_keys
session show
session unset workflow
session reset
```

- **Creation is implicit.** The client chooses the `session` string; the
  server creates it on first use. Idle sessions expire after
  `limits.session_ttl_s`; an expired id becomes a fresh empty session, not
  an error.
- **Bound to caller identity.** The same session string from a different
  caller is a different session. This is what makes the next rule
  enforceable: sessions MUST NOT be a vehicle for privilege — permissions
  are evaluated per invocation.
- `KEY` in `session set` MUST be either a `from_session` name declared by
  some command, or a name in `manifest.session_keys`. Otherwise
  `invalid_args` with `did_you_mean`.
- Values are stored as strings and coerced at use time by the receiving
  flag's type. A coercion failure at use → `invalid_value` with
  `details.source: "session"`.
- A flag with `from_session` falls back to the session value when omitted.
  Explicit argv always wins.
- `session show` → `data: { "id", "values": {…}, "expires" }`.
  `session reset` clears values and keeps the id.
- A server without the `sessions` level MUST ignore the `session` field.

### 4.10 Handles

Commands return **handles** — opaque, typed ids — so that outputs of one
command become inputs of another without the agent copying large data
through its context (the ACI replacement for pipes).

```json
"handles": [ { "id": "r_42", "type": "run", "label": "wing / 2026-09-21", "expires": null } ]
```

- Types `blob` and `table` are protocol-defined; all other types are
  application-defined lowercase identifiers. Handle ids MUST be unique
  across all types within the application.
- A flag of type `handle:run` accepts any handle whose `type` is `run`.
- **Lifetime.** `expires: null` = persistent, backed by a domain object;
  survives sessions; `handle release` on it is a no-op (`ok: true` with a
  warning). `expires: <RFC 3339>` = temporary, materialized output;
  released explicitly or at expiry (default `limits.handle_ttl_s`), then
  `not_found`.
- **Scope.** Temporary handles are visible only to the caller identity that
  produced them. Persistent handles follow the application's own
  authorization. Sessions do not own handles.
- `size`: bytes for `blob`, row count for `table`, omitted otherwise.
- `handle show ID` returns the Handle plus `accepted_by`: every visible
  command with a param whose type includes `handle:<type>`.
- `handle read ID [--range a:b] [--fields …]` — paged access:
  - `table`: `--range` is a half-open row range; `--fields` projects
    columns; `data: { "columns": [...], "rows": [...] }`; default page
    `limits.default_limit` rows; `truncated` as usual.
  - `blob`: `--range` is a byte range; `data: { "encoding": "utf8"|"base64",
    "content": "…", "total": <bytes> }`; the server MUST NOT return more
    than `limits.max_read_bytes` per call.
  - Any other type → `invalid_args`, hint naming the commands that accept it.
- `handle release ID` frees a temporary handle; persistent domain objects
  are unaffected.
- Large outputs (tables, files, logs) MUST be returned as `blob`/`table`
  handles rather than inlined past the §4.11 budget.

### 4.11 Output shaping and context budget

The server is responsible for keeping responses small by default.

| Rule | Requirement |
|---|---|
| `summary` | MUST be present on every success; SHOULD be ≤ 3 sentences. |
| `data` default size | SHOULD be ≤ ~2 KB serialized for `--format summary`; lists default to `limits.default_limit`. A list that would exceed it MUST paginate; a large object SHOULD become a handle (§4.10). |
| Truncation | MUST be signalled in `truncated`. Silent truncation is forbidden. |
| `--fields` | MUST be supported for object/list `data`. |
| `--format text` | SHOULD render tables in aligned monospace. `next` and `fix` are rendered as quoted `line` strings (e.g. `workflow delete wf_opt11 --yes`) so a model can copy them verbatim. On ordinary commands `text` accompanies `data`; on discovery commands they are exclusive (§4.2). `--quiet` strips `text`. |
| `help` node (text) | SHOULD fit in ~40 lines / ~600 tokens. |
| `--help` descriptor (text) | SHOULD fit in ~100 lines / ~1500 tokens; move long prose to `scenario show`. JSON forms SHOULD stay within 2× of the text budget. |
| `search` | ≤ 10 hits default. |
| `explore` | Full steps for the top scenario only. |

**Pagination.**

- `--limit` above `limits.max_limit` is clamped with a `warnings` entry;
  `0` or negative → `invalid_value`.
- Cursors are opaque, valid for at least `limits.cursor_ttl_s`, and bound to
  the (command, argv-minus-cursor) that produced them. An expired,
  malformed or foreign cursor → `invalid_value` with
  `details.flag: "cursor"` and a `fix` that re-runs without it.
- `truncated` keys are dotted paths into `data`. For a list: `shown`,
  `total` (`null` when unknown), `cursor` (absent when there is no next
  page). For a string cut for size: `shown`/`total` in bytes, no cursor;
  the hint is to use `handle read`.
- `--fields` uses the same path syntax as `@input.`; a path crossing an
  array applies to each element. Unknown path → `invalid_value` with
  `details.available`. Projection happens after pagination, so `truncated`
  reflects the unprojected list.

Rationale: these budgets make the *incremental* cost of each discovery step
predictable, which is what makes runtime discovery cheaper than eager
registration.

### 4.12 Effects, dry-run and confirmation

- Every command declares `effects`; an empty array is a pure read. Clients
  MAY use this to apply their own approval policies (e.g. auto-run empty
  `effects`, ask before `destructive`).
- Commands with `destructive` or `billing` effects MUST refuse without
  `--yes`, returning `confirmation_required` whose `details.plan` is the
  same description a `--dry-run` would produce. `--yes` on a command that
  does not need it is ignored silently.
- `--dry-run` MUST be supported by every command. On a command with
  effects it validates arguments, permissions and preconditions and returns
  `ok: true` with `data.plan`: a human-readable description of what would
  happen. It MUST NOT produce any effect in the declared set, but MAY
  perform reads (including external reads) to validate. On a pure read it
  returns `data.plan: "read-only; no effects"` without executing.
  `--dry-run --yes` together: dry-run wins.

**Idempotency.** Servers SHOULD honour `options.idempotency_key` so that
agent retries do not duplicate effects:

- Keys are scoped to the caller identity and apply to every command with
  non-empty `effects`; on pure reads they are ignored.
- The fingerprint is the resolved path + normalized argv (after session
  defaults and `@input` substitution) + `input` + `dry_run`/`confirm`.
  Same key and fingerprint within `limits.idempotency_ttl_s` → the server
  MUST return the **original response verbatim** (same `task.id`, same
  `handles`). Same key, different fingerprint → `conflict` with
  `details.reason: "idempotency_mismatch"`.
- If the original is still executing, the server MAY block and return its
  result, or return `conflict` with `retryable: true` and
  `details.retry_after_ms`.
- A `--dry-run` never consumes a key: dry-run then real call with the same
  key is the intended pattern.

### 4.13 Permissions

Descriptors list `permissions` (opaque strings, application-defined).
For each command and caller the application assigns one of three states:

| State | `help` listing | `--help`, `search`, scenarios, `related`, `next`, `did_you_mean` | `invoke` |
|---|---|---|---|
| `visible` | listed | full descriptor | runs |
| `locked` | listed with `locked: true` | full descriptor (so the agent can read `permissions` and how to obtain them) | `permission_denied` |
| `hidden` | not listed | never appears; `--help` → `unknown_command` | `unknown_command` |

- A group is `hidden` iff all its children are hidden; otherwise it is
  listed without its hidden children.
- A scenario is hidden iff any of its `commands` is hidden. Scenarios with
  `locked` steps are shown; their aggregate `permissions` tell the agent
  what is missing before it starts.
- `permission_denied.details.required` lists only the *missing*
  permissions; `details.how_to_obtain` (text or URL) is optional and MAY be
  resolved from `manifest.permissions`.
- `hidden` yields `unknown_command`, not `not_found`, so a hidden command
  is indistinguishable from a nonexistent one.
- Nothing in `help`, `scenario` or `search` output grants anything; they are
  descriptions. Every `invoke` is authorized independently.

### 4.14 Error catalogue

| Code | Meaning | MUST include |
|---|---|---|
| `unsupported_version` | Request `aci` not accepted. | `details.supported` |
| `bad_request` | Malformed envelope. | `hint` |
| `shell_syntax_rejected` | `line` contained unquoted shell operators. | `hint` explaining quoting, `@input`, and placeholders |
| `unknown_command` | Path does not resolve (or is hidden). | `did_you_mean` (≤ 3 paths), `help` pointing to the nearest resolved node |
| `invalid_args` | Unknown flag, repeated scalar, bad shape, unterminated quote. | `hint`, `help`; `did_you_mean` for unknown flags |
| `missing_arg` | Required arg/flag/value absent. | `details.missing`, `fix` |
| `invalid_value` | Value does not coerce, bad `@input` ref, bad cursor/fields. | `details.flag`, `details.expected` |
| `permission_denied` | Caller lacks permission for a `locked` command. | `details.required` |
| `not_found` | A referenced object/handle/task does not exist or is not visible. | `details.ref`, `hint` |
| `confirmation_required` | Destructive/billing without `--yes`. | `details.plan`, `fix` with `--yes` |
| `precondition_failed` | Domain precondition unmet. | `hint`, `fix` (an argv to satisfy it) |
| `conflict` | State changed concurrently / idempotency mismatch / cancel of finished task. | `hint` |
| `rate_limited` | Too many requests. | `details.retry_after_ms`, `retryable: true` |
| `timeout` | Sync execution exceeded timeout on a non-`async` command. | `retryable` |
| `task_failed` | Async work failed (inside `task.result`). | `details.task`, `hint` |
| `unavailable` | Backend down. | `retryable: true` |
| `internal` | Unexpected server error. | `details.trace_id` |

All errors MUST include `code`, `message`, `hint`. `fix` is an array of argv
alternatives (templates; MAY contain `<placeholder>`); the first is the most
likely. `help` is an argv that fetches the relevant descriptor.
`did_you_mean` is an array of argv fragments.

**Precedence.** Validation proceeds in this order and the server MUST
report the first class that fails (it MAY list several errors of the same
class in `details`):

```
unsupported_version → bad_request → shell_syntax_rejected → unknown_command
→ invalid_args | missing_arg | invalid_value → permission_denied
→ not_found → confirmation_required → precondition_failed
→ domain errors (conflict, rate_limited, timeout, unavailable, internal)
```

---

## 5. Transport bindings

All bindings carry the same envelope (§4.1). Authentication is out of scope
for ACI; bindings reuse their transport's conventions. A server MUST
implement at least one binding and document which; the single-tool binding
(§5.4) is RECOMMENDED.

### 5.1 HTTP

| Method & path | Body / response |
|---|---|
| `GET {base}/` | Manifest (same as `info`). Also serves as liveness. |
| `POST {base}/invoke` | Request envelope → response envelope. `Content-Type: application/json`. |
| `GET {base}/tasks/{id}/events` | Optional `text/event-stream` of task events. |

HTTP status mapping:

| Status | When |
|---|---|
| `200` | Any envelope the server could parse and route — including `ok: false` domain errors such as `permission_denied`, `not_found`, `confirmation_required`. |
| `400` | `bad_request`, `unsupported_version` (envelope in body). |
| `401` / `403` | The transport could not authenticate the caller at all. No envelope guaranteed. |
| `429` | `rate_limited` envelope, plus `Retry-After`. |
| `503` | `unavailable` envelope. |
| `500` | `internal` envelope. |

SSE events: `event: progress|log|done`, `data:` is JSON, `id:` is the same
cursor that `task logs --since` accepts, so `Last-Event-ID` resumes a
stream. `done` carries the full Task object including `result`.

### 5.2 JSON-RPC 2.0

- Method `aci.invoke`, params = request envelope, result = response envelope.
- Domain errors are returned as a *result* with `ok: false`, not as JSON-RPC
  errors; JSON-RPC errors are reserved for protocol-level failures
  (`bad_request` and `unsupported_version` MAY be mapped to them).
- Notification `aci.task.event` with `{ "task": "t_918", "event": "progress", "data": {…} }`.
  Notifications require a bidirectional transport (WebSocket, stdio); over
  plain HTTP the binding is request/response only and events fall back to
  SSE (§5.1).

### 5.3 stdio

Newline-delimited JSON, one envelope per line, over a child process's
stdin/stdout. Suitable for local tools and for wrapping ACI in an
editor/agent host. Requests SHOULD carry `id` so responses can be
correlated when pipelined. Task events are emitted as lines with `"event"`
and `"task"` in place of `"ok"`.

### 5.4 Single-tool binding (LLM tool-use APIs and MCP)

An ACI application is exposed to a model as **exactly one tool**, whether
through a provider's native tool-use / function-calling API or through an
MCP server. The parameter schema, in JSON Schema:

```json
{
  "name": "engineering",
  "description": "<bootstrap text, §6>",
  "schema": {
    "type": "object",
    "properties": {
      "line":    { "type": "string", "description": "The command as one line, e.g. \"workflow run --workflow wing\". Preferred." },
      "argv":    { "type": "array", "items": { "type": "string" }, "description": "Alternative to line: pre-split tokens." },
      "input":   { "type": "object", "description": "Structured values referenced from the command as @input.<key>." },
      "session": { "type": "string" }
    }
  }
}
```

- Exactly one of `line`/`argv` is expected; `options` are reachable through
  global flags.
- The key under which a provider carries the schema (`input_schema`,
  `parameters`, …) and any provider "strict" mode are host-specific and
  outside ACI.
- **Rendering is the host's choice.** The tool result is the response
  envelope serialized as JSON text by default. Hosts serving models with
  weak tool calling SHOULD set `options.format: "text"` on every call and
  return `text` (falling back to `summary` when `text` is absent) instead of
  the JSON envelope. P10 guarantees the model can act on that alone.
- Context cost in the model is constant per application, however large
  its tree. MCP is one host for this definition; the definition itself does
  not depend on it.

### 5.5 Generated CLI

A binary CLI can be generated from (or thin-wrapped around) any ACI server:
`app <argv…>` sends `argv` as-is and prints `text` (or `summary` + `data`).
Humans and agents share one interface, one help system, one set of examples.
This is the recommended way to *test* an ACI tree: if a person cannot use
it from the terminal, an agent will struggle too.

---

## 6. Bootstrap text

The constant text a client puts in the agent's context. Servers SHOULD
provide it in `info.bootstrap` (≤ ~200 tokens); when absent, clients use
this generic form with `{name}` substituted:

```
You can use the application "{name}" by sending one command line at a time.
- `help` lists top-level groups and commands; `help <group>` lists a group.
- `<command> --help` shows arguments, examples, side effects and permissions.
- `search "<what you want to do>"` finds commands and scenarios by intent.
- `explore "<goal>"` returns a step-by-step scenario to follow.
- Long operations return a task id: `task wait <id>`, `task logs <id>`.
- Pass large or structured values via `input` and reference them as @input.<key>.
Every result has a `summary`, optional `data`, `handles` you can pass to other
commands, and `next` suggestions. Errors include a `hint` and often a `fix`
argv you can run directly. Destructive commands need `--yes`; use `--dry-run`
to preview.
```

≈ 130 tokens.

---

## 7. Example session

```
> info
  engineering 3.2.0 — Build, validate and run simulation and optimization workflows.
  top scenarios: optimize-geometry, import-cfd-model, compare-runs

> explore "optimize wing geometry using my existing CFD model"
  Scenario optimize-geometry (score 0.93)
  prerequisites: [x] CFD model imported   (check: simulation list --kind cfd)
  placeholders: model (user), workflow (step 2), task (step 4), run (step 5)
  steps: 1 simulation inspect <model> --parameters
         2 optimization configure --model <model> --vars @input.vars --objectives @input.objectives
         3 workflow validate <workflow>
         4 workflow run --workflow <workflow>        (async)
         5 task wait <task>
         6 results pareto <run> --explain

> simulation list --kind cfd
  summary: 1 CFD model: wing_v3 (sim_wing3).
  handles: [sim_wing3: simulation]

> optimization configure --help
  … flags: --model handle:simulation (required), --vars json, --objectives json, --algorithm enum[…]
  examples: …

> optimization configure --model sim_wing3 --vars @input.vars --objectives @input.objectives
  input: { vars: ["chord","twist"], objectives: [{"name":"lift_drag","goal":"max"}] }
  summary: Created optimization study opt_11 (2 variables, 1 objective, algorithm nsga2). Workflow wf_opt11 generated.
  handles: [opt_11: optimization, wf_opt11: workflow]
  next: workflow validate wf_opt11

> workflow validate wf_opt11
  summary: Valid. 1 warning: 'twist' has no bounds; defaults to ±5°.

> workflow run --workflow wf_opt11
  summary: Started run r_42 (est. 4 min).
  task: t_918 running
  next: task wait t_918

> task wait t_918 --timeout 60000
  task: t_918 running 35% "generation 7/20"

> task wait t_918 --timeout 60000
  task: t_918 succeeded
  result.summary: Run r_42 finished: 20 generations, 240 evaluations, 9 Pareto points.
  handles: [r_42: run, tbl_r42: table]

> results pareto r_42 --explain --limit 3
  summary: 9 Pareto points; best lift/drag 18.4 at chord=1.12, twist=+2.1°.
  data: [ …3 rows… ]  truncated: data.rows shown 3 of 9, cursor "c3"

> workflow delete wf_opt11
  error confirmation_required: Deleting workflow wf_opt11 removes 1 run (r_42) and its results.
  fix: workflow delete wf_opt11 --yes
```

---

## 8. Authoring guidelines for application developers

1. **Start from scenarios, not tables.** List the 10–20 things users come to
   the application to do. Each becomes a scenario; the commands are its
   steps.
2. **Litmus test for a command:** "Would a user type this in a terminal?"
   `workflow run` yes; `execution-record create` no.
3. **One command = one meaningful step.** A command may touch several
   internal resources. Hide joins, foreign keys and status polling behind it.
4. **Verbs users use.** `import`, `validate`, `run`, `compare`, `explain`,
   `promote`, `rollback` — not `create`, `update`, `patch`.
5. **Shape the tree for humans:** 5–9 children per node, depth ≤ 3.
6. **Summaries ≤ 80 chars, examples mandatory, errors with fixes.** Write
   the `errors` section of each descriptor before the implementation.
7. **Return handles, not dumps.** If a result could exceed a screen, return
   a handle and a summary.
8. **Make `--dry-run` real.** It is how agents (and their operators) build
   trust.
9. **Keep the conventional API.** ACI is a semantic layer over REST/GraphQL/
   queues; it does not replace them.
10. **Test with the generated CLI.** If `app help` reads badly to you, fix
    the tree, not the prompt.
11. **Bump `version` when the tree changes.** Clients cache descriptors
    against it (§9).

---

## 9. Relationship to other protocols

| Protocol | Relationship |
|---|---|
| **Native tool calling** | ACI is exposed as one tool whose description is the bootstrap text (§5.4). The tree, `help` and `search` replace a growing tool list. |
| **MCP** | One host for the single-tool binding. ACI's tree/help/search replaces eager tool registration. |
| **OpenAPI / GraphQL** | Typical backends behind an ACI server. ACI descriptors may reference their schemas via `schema_ref`. |
| **Agent Skills** | An ACI scenario is the executable counterpart of a skill's procedure. Skills can point at ACI commands. |
| **ACP (Agent Client Protocol)** | Orthogonal: ACP connects editors to coding agents; those agents may call ACI applications. |
| **A2A** | An A2A Agent Card can advertise an ACI endpoint as the way to work with the application directly. |
| **Classic CLI** | ACI is a CLI's interface with the shell removed and the output made structural. A CLI can be generated from it (§5.5). |

**Caching.** A client MAY cache `help`, `--help` and `scenario` results
while the manifest `version` is unchanged; a server MUST bump `version`
whenever the tree or any descriptor changes.

---

## 10. Conformance checklist

**Core**
- [ ] `invoke` over at least one binding; `argv` and `line` both accepted; unquoted shell operators rejected, quoted ones accepted.
- [ ] `aci` version rule and `unsupported_version`; malformed envelopes answered with `bad_request`.
- [ ] `id` echoed when present; `command` is the deepest resolved node, `""` if none.
- [ ] `info` returns manifest with `levels`; absent `limits` keys mean the A.9 defaults.
- [ ] `help`, `help PATH`, `<path> --help` in `text` and `json`; bare group path behaves as `help`.
- [ ] Every command has `summary` ≤ 80 chars, `effects`, and ≥ 1 example; node names match `^[a-z][a-z0-9-]*$`.
- [ ] Flag rules of §4.3 (bool never consumes; scalar repeat rejected; `-[0-9]` is a value; no bundling).
- [ ] `@input` substitution and coercion per §4.3.3 / §4.4.1; `@@` escape.
- [ ] Every success has `summary`; truncation signalled with `shown`/`total`/`cursor`; `--limit` clamped to `max_limit`.
- [ ] Every error has `code`, `message`, `hint`; `unknown_command` has `did_you_mean`; precedence order of §4.14 respected.
- [ ] `--dry-run` on all commands; `--yes` gate on destructive/billing.
- [ ] Global flags reserved (tree rejected otherwise) and honoured; `options` mapping of §4.3.4.
- [ ] Permission states `visible`/`locked`/`hidden` behave per §4.13.

**Search** — `search` with `--limit`, ≤ 10 default hits, summaries only, hit shape of §4.7.
**Scenarios** — `explore` ≡ `search --kind scenario`; `scenario list|show`; steps are argv arrays; `placeholders` declared.
**Tasks** — `task status|wait|cancel|logs|list`; `ok: true` for failed tasks with the error inside `task.result`; caller-scoped; retention ≥ `task_retention_s`.
**Sessions** — implicit creation; caller-bound; keys restricted to `from_session` names and `session_keys`; `from_session` fallback; no privilege via session.
**Handles** — unique ids; `expires` null/temporary semantics; `handle show|read|release`; `blob`/`table` read shapes; `max_read_bytes`.
**Idempotency** — verbatim replay; `conflict` on mismatch; dry-runs do not consume keys.
**Streaming** — task events on the binding; SSE `id` = log cursor.

---

## 11. Security considerations

- **Not a shell.** Servers MUST dispatch only registered commands. `line`
  lexing is lexical only; there is no expansion of any kind.
- **Help content is data.** For third-party ACI servers, clients SHOULD treat
  `summary`, `hint`, `next`, `scenario` text as untrusted content (prompt
  injection surface), and MAY apply their own approval policies based on
  `effects` regardless of what the server suggests.
- **Authorization at execution.** Permissions listed in descriptors are
  informational. Every `invoke` is authorized independently.
- **Existence is not leaked.** Hidden commands, other callers' tasks,
  temporary handles and sessions are indistinguishable from nonexistent
  ones.
- **Isolation is out of scope.** ACI expresses effects and permissions; it
  does not provide sandboxing, network policy, or tenant isolation. Those
  belong to the deployment.
- **Audit.** Servers SHOULD log `argv`, caller, `effects`, and result code for
  every invocation with non-empty `effects`.

---

## 12. Open questions

Not part of conformance for 0.2.

1. **Partial output for long synchronous commands** — stream `data` chunks or
   always convert to a task?
2. **Federation** — a gateway whose root groups are applications; should
   `search` span applications, and how are handles namespaced?
3. **Scenario execution on the server** — keep as vendor extension, or define
   a `scenario run` with checkpoints and approvals?
4. **Client-side files** — `@file:` references would let agents upload
   content; needs a transport-level attachment mechanism.
5. **Localization** of summaries and hints.
6. **Descriptor signing** — attestation of help/scenario content for
   third-party servers.
7. **Shared tasks** — tasks are caller-scoped; team-visible tasks may need a
   sharing model.

---

## Appendix A — JSON Schemas (compact)

### A.1 Request

```json
{
  "type": "object",
  "required": ["aci"],
  "properties": {
    "aci":     { "type": "string", "pattern": "^[0-9]+\\.[0-9]+$" },
    "id":      { "type": "string" },
    "argv":    { "type": "array", "items": { "type": "string" }, "minItems": 1 },
    "line":    { "type": "string" },
    "input":   { "type": "object" },
    "session": { "type": "string" },
    "options": {
      "type": "object",
      "properties": {
        "format":          { "enum": ["summary", "json", "text"] },
        "limit":           { "type": "integer", "minimum": 1 },
        "cursor":          { "type": "string" },
        "fields":          { "type": "array", "items": { "type": "string" } },
        "timeout_ms":      { "type": "integer", "minimum": 1 },
        "idempotency_key": { "type": "string" },
        "dry_run":         { "type": "boolean" },
        "confirm":         { "type": "boolean" },
        "async":           { "type": "boolean" },
        "quiet":           { "type": "boolean" }
      }
    }
  },
  "oneOf": [ { "required": ["argv"] }, { "required": ["line"] } ]
}
```

### A.2 Response

```json
{
  "type": "object",
  "required": ["aci", "ok", "command"],
  "properties": {
    "aci":       { "type": "string" },
    "id":        { "type": "string" },
    "ok":        { "type": "boolean" },
    "command":   { "type": "string" },
    "summary":   { "type": "string" },
    "data":      {},
    "text":      { "type": "string" },
    "handles":   { "type": "array", "items": { "$ref": "#/A.6" } },
    "task":      { "$ref": "#/A.7" },
    "next":      { "type": "array", "items": {
                   "type": "object", "required": ["argv"],
                   "properties": { "argv": { "type": "array", "items": { "type": "string" } },
                                   "why": { "type": "string" } } } },
    "warnings":  { "type": "array", "items": { "type": "string" } },
    "truncated": { "type": ["object", "null"],
                   "additionalProperties": { "type": "object",
                     "properties": { "shown": { "type": "integer" }, "total": { "type": ["integer", "null"] },
                                     "cursor": { "type": "string" } } } },
    "error":     { "$ref": "#/A.3" }
  }
}
```

### A.3 Error

```json
{
  "type": "object",
  "required": ["code", "message", "hint"],
  "properties": {
    "code":         { "type": "string" },
    "message":      { "type": "string" },
    "hint":         { "type": "string" },
    "fix":          { "type": "array", "items": { "type": "array", "items": { "type": "string" } } },
    "did_you_mean": { "type": ["array", "null"], "items": { "type": "array", "items": { "type": "string" } } },
    "help":         { "type": "array", "items": { "type": "string" } },
    "retryable":    { "type": "boolean" },
    "details":      { "type": "object" }
  }
}
```

### A.4 CommandDescriptor

```json
{
  "type": "object",
  "required": ["path", "summary", "usage", "flags", "args", "examples", "effects"],
  "properties": {
    "path":        { "type": "array", "items": { "type": "string" } },
    "summary":     { "type": "string", "maxLength": 80 },
    "description": { "type": "string" },
    "usage":       { "type": "string" },
    "args":        { "type": "array", "items": { "$ref": "#/A.4.param" } },
    "flags":       { "type": "array", "items": { "$ref": "#/A.4.param" } },
    "input":       { "type": "object", "properties": { "schema_ref": { "type": "string" }, "schema": {}, "help": { "type": "string" } } },
    "output":      { "type": "object", "properties": { "summary_of": { "type": "string" }, "schema_ref": { "type": "string" }, "schema": {},
                                                       "handles": { "type": "array", "items": { "type": "string" } } } },
    "examples":    { "type": "array", "minItems": 1, "items": { "type": "object", "required": ["argv"],
                     "properties": { "argv": { "type": "array", "items": { "type": "string" } }, "input": { "type": "object" }, "comment": { "type": "string" } } } },
    "effects":     { "type": "array", "items": { "enum": ["write", "destructive", "external", "billing"] } },
    "async":       { "type": "boolean" },
    "permissions": { "type": "array", "items": { "type": "string" } },
    "preconditions": { "type": "array", "items": { "type": "object", "required": ["text"],
                       "properties": { "text": { "type": "string" }, "check": { "type": ["array", "null"], "items": { "type": "string" } } } } },
    "errors":      { "type": "array", "items": { "type": "object", "required": ["code", "when"],
                     "properties": { "code": { "type": "string" }, "when": { "type": "string" }, "fix": { "type": "array", "items": { "type": "string" } } } } },
    "related":     { "type": "array", "items": { "type": "array", "items": { "type": "string" } } },
    "scenarios":   { "type": "array", "items": { "type": "string" } },
    "stability":   { "enum": ["experimental", "stable", "deprecated"] },
    "replaced_by": { "type": "array", "items": { "type": "string" } },
    "since":       { "type": "string" }
  }
}
```

`#/A.4.param`:

```json
{
  "type": "object",
  "required": ["name", "type"],
  "properties": {
    "name":         { "type": "string" },
    "short":        { "type": "string", "pattern": "^[a-gi-zA-Z]$" },
    "type":         { "type": "string" },
    "values":       { "type": "array", "items": { "type": "string" } },
    "required":     { "type": "boolean" },
    "default":      {},
    "repeatable":   { "type": "boolean" },
    "help":         { "type": "string" },
    "from_session": { "type": "string" }
  }
}
```

### A.5 TreeNode

```json
{
  "type": "object",
  "required": ["path", "kind", "summary", "children"],
  "properties": {
    "path":     { "type": "array", "items": { "type": "string" } },
    "kind":     { "enum": ["group", "command"] },
    "summary":  { "type": "string", "maxLength": 80 },
    "children": { "type": "array", "items": { "type": "object", "required": ["name", "kind", "summary"],
                  "properties": { "name": { "type": "string", "pattern": "^[a-z][a-z0-9-]*$" }, "kind": { "enum": ["group", "command"] },
                                  "summary": { "type": "string" }, "effects": { "type": "array" },
                                  "locked": { "type": "boolean" } } } },
    "scenarios": { "type": "array", "items": { "type": "string" } }
  }
}
```

### A.6 Handle

```json
{
  "type": "object",
  "required": ["id", "type"],
  "properties": {
    "id":      { "type": "string" },
    "type":    { "type": "string", "pattern": "^[a-z][a-z0-9-]*$" },
    "label":   { "type": "string" },
    "size":    { "type": "integer" },
    "expires": { "type": ["string", "null"], "format": "date-time" }
  }
}
```

### A.7 Task

```json
{
  "type": "object",
  "required": ["id", "state"],
  "properties": {
    "id":       { "type": "string" },
    "state":    { "enum": ["queued", "running", "succeeded", "failed", "cancelled"] },
    "progress": { "type": "object", "properties": { "percent": { "type": "number" }, "message": { "type": "string" } } },
    "started":  { "type": "string", "format": "date-time" },
    "finished": { "type": ["string", "null"], "format": "date-time" },
    "result":   { "$ref": "#/A.2" }
  }
}
```

### A.8 Scenario

```json
{
  "type": "object",
  "required": ["name", "title", "intents", "summary", "steps"],
  "properties": {
    "name":          { "type": "string", "pattern": "^[a-z][a-z0-9-]*$" },
    "title":         { "type": "string" },
    "intents":       { "type": "array", "items": { "type": "string" } },
    "summary":       { "type": "string" },
    "when":          { "type": "string" },
    "prerequisites": { "type": "array", "items": { "type": "object", "required": ["text"],
                       "properties": { "text": { "type": "string" }, "check": { "type": ["array", "null"], "items": { "type": "string" } } } } },
    "placeholders":  { "type": "array", "items": { "type": "object", "required": ["name"],
                       "properties": { "name": { "type": "string" }, "type": { "type": "string" },
                                       "from": { "type": "string", "pattern": "^(user|step:[0-9]+)$" }, "help": { "type": "string" } } } },
    "steps":         { "type": "array", "minItems": 1, "items": { "type": "object", "required": ["do"],
                       "properties": { "do": { "type": "array", "items": { "type": "string" } },
                                       "expect": { "type": "string" }, "if_fails": { "type": "string" }, "async": { "type": "boolean" } } } },
    "outcomes":      { "type": "array", "items": { "type": "string" } },
    "permissions":   { "type": "array", "items": { "type": "string" } },
    "commands":      { "type": "array", "items": { "type": "array", "items": { "type": "string" } } }
  }
}
```

### A.9 Manifest

```json
{
  "type": "object",
  "required": ["name", "title", "version", "aci", "levels", "description"],
  "properties": {
    "name":          { "type": "string", "pattern": "^[a-z][a-z0-9-]*$" },
    "title":         { "type": "string" },
    "version":       { "type": "string" },
    "aci":           { "type": "string" },
    "levels":        { "type": "array", "items": { "enum": ["core", "search", "scenarios", "tasks", "sessions", "handles", "streaming"] } },
    "description":   { "type": "string" },
    "top_scenarios": { "type": "array", "items": { "type": "string" } },
    "bootstrap":     { "type": "string" },
    "auth":          { "type": "object", "properties": { "scheme": { "type": "string" }, "obtain": { "type": "string" } } },
    "permissions":   { "type": "array", "items": { "type": "object", "required": ["name"],
                       "properties": { "name": { "type": "string" }, "help": { "type": "string" }, "obtain": { "type": "string" } } } },
    "session_keys":  { "type": "array", "items": { "type": "object", "required": ["name"],
                       "properties": { "name": { "type": "string" }, "help": { "type": "string" } } } },
    "limits":        { "type": "object", "properties": {
      "max_help_depth":     { "type": "integer" },
      "max_limit":          { "type": "integer" },
      "default_limit":      { "type": "integer" },
      "default_timeout_ms": { "type": "integer" },
      "task_wait_max_ms":   { "type": "integer" },
      "task_retention_s":   { "type": "integer" },
      "handle_ttl_s":       { "type": "integer" },
      "max_read_bytes":     { "type": "integer" },
      "session_ttl_s":      { "type": "integer" },
      "idempotency_ttl_s":  { "type": "integer" },
      "cursor_ttl_s":       { "type": "integer" }
    } }
  }
}
```

`levels` MUST contain `core`. A `limits` key that is absent has the
following normative default, which clients MAY assume:

| Key | Default |
|---|---|
| `max_help_depth` | 2 |
| `max_limit` | 200 |
| `default_limit` | 20 |
| `default_timeout_ms` | 10000 |
| `task_wait_max_ms` | 30000 |
| `task_retention_s` | 86400 |
| `handle_ttl_s` | 3600 |
| `max_read_bytes` | 65536 |
| `session_ttl_s` | 86400 |
| `idempotency_ttl_s` | 86400 |
| `cursor_ttl_s` | 3600 |

---

## Appendix B — Text rendering of `--help`

For `--format text`, servers SHOULD render descriptors in the familiar CLI
shape so the same output serves terminals and models:

```
workflow run — Execute a workflow and start an observable run.

Usage:
  workflow run --workflow <name|handle> [--params @input.params] [--priority low|normal|high]

Flags:
  --workflow <handle:workflow|string>   Workflow name or handle. (required; session default: workflow)
  --params   <json>                     Parameter overrides. Usually @input.params.
  --priority <low|normal|high>          Default: normal.

Effects: write, external   Async: yes   Permissions: workflow.read, execution.run

Preconditions:
  - Workflow must pass validation.   check: workflow validate <workflow>

Examples:
  workflow run --workflow wing
      Run with stored parameters.
  workflow run --workflow wing --params @input.params
      Override parameters (input: {"params": {"mach": 0.8}}).

Errors:
  precondition_failed   Workflow has validation errors.   fix: workflow validate <workflow> --explain

Related: workflow validate, workflow results, task wait
Scenarios: optimize-geometry
```

---

## Appendix C — Changes from Draft 0.1

- Strict group/command dichotomy; node-name grammar; bare group path
  returns `help`.
- `line` lexer defined; shell operators rejected only when unquoted.
- Flag/positional rules (§4.3) made normative; global flag names reserved
  at tree load; `options` ↔ flag mapping.
- `@input` substitution semantics and `@@` escape.
- Parameter coercion table (§4.4.1); union try-order.
- `read` removed from `effects`; empty array is the read-only state.
- `--dry-run` on every command; error precedence order; new codes
  `unsupported_version`, `bad_request`.
- Request `id` echo; `command` = deepest resolved node; version rule.
- Discovery commands return `text` XOR `data`.
- Tasks: conversion rule, caller scoping, retention, `ok: true` for failed
  tasks with the error in `task.result`.
- Sessions: implicit creation, caller binding, restricted keys,
  `manifest.session_keys`.
- Handles: reserved `blob`/`table`, lifetime by `expires`, `handle read`
  shapes, `max_read_bytes`.
- Idempotency: verbatim replay, fingerprint, `conflict` on mismatch.
- Pagination: clamping, cursor binding, `total: null`, `--fields` semantics.
- Permissions: `visible` / `locked` / `hidden` states.
- Scenarios: argv arrays only, declared `placeholders`, `explore` defined
  via `search`.
- Transports: HTTP status table, SSE resume, §5.4 generalized to any LLM
  tool-use API with MCP as one host.
- Manifest schema (A.9) with normative `limits` defaults; descriptor
  caching keyed on `version`; `replaced_by` for deprecations.
- P10 model-agnostic: `line` is the primary form; host-chosen `text`
  rendering; `next`/`fix` rendered as lines; lexer tolerance for echoed
  app name and prompt glyphs; provider-neutral single-tool schema.
