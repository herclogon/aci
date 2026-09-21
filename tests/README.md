# LLM evaluation harness

Checks whether a given model can drive an AAI application using only the
protocol's own guidance: the single tool from SPEC.md §5.4, the bootstrap
text from §6, and the system prompt in `../AGENT_PROMPT.md`.

## Files

| File | Role |
|---|---|
| `mock_app.json` | Fixture application: tree (4 groups, 10 commands), 2 scenarios, initial state. |
| `aai_mock.py` | In-process AAI server over the fixture — lexing, flag rules, `@input`, coercion, help, search, tasks, `--dry-run`/`--yes`, error catalogue. `render_text()` is the host-side text rendering. |
| `cases.json` | Eval cases: user turns, pass checks, call budget. |
| `scripted_agents.py` | Golden script per case (proves each case is solvable). |
| `llm_eval.py` | Runner + provider adapters (`anthropic`, `openai`-compatible, `openrouter`, `scripted`). |
| `qwen_openrouter.py` | Catalog-validated OpenRouter sweep across descending Qwen model sizes. |
| `test_harness.py` | Unit tests for the mock's spec behaviour and the harness scoring. |
| `results/` | JSON reports, one per run. |

## Run

```sh
python -m unittest tests/test_harness.py            # self-check, no keys needed
python tests/llm_eval.py --provider scripted        # golden agents through the real loop

pip install anthropic
export ANTHROPIC_API_KEY=...
python tests/llm_eval.py --provider anthropic --model claude-opus-5
python tests/llm_eval.py --provider anthropic --model claude-haiku-4-5 --repeat 3

pip install openai
python tests/llm_eval.py --provider openai --model gpt-4o-mini            # OPENAI_API_KEY
python tests/llm_eval.py --provider openai --model llama3.1:8b --base-url http://localhost:11434/v1   # Ollama
python tests/llm_eval.py --provider openai --model qwen2.5:7b --base-url http://localhost:11434/v1 --format json

export OPENROUTER_API_KEY=...
python tests/llm_eval.py --provider openrouter --model qwen/qwen3-32b
python tests/qwen_openrouter.py
```

The default Qwen sweep requests 32B, 27B, 14B, 9B, 8B, 7B, and 4B model
slugs. It reads OpenRouter's live catalog first and skips discontinued or
temporarily unavailable entries. Use repeated `--model` flags to choose a
different set, and the regular `--case`, `--repeat`, `--format`, and `-v`
options to control the evaluation.

`--format text` (default) shows the model the host-rendered text of each
result; `--format json` shows the raw envelope. Comparing the two for the
same model tells you whether P10's "host chooses text" rule is load-bearing
for that model. `-v` prints the full exchange. `--repeat N` runs each case N
times; models are stochastic, so report pass rates, not single runs.

## Scoring

A case **passes** when every check holds, no hard violation occurred, and
the call budget was not exhausted.

Checks (`cases.json`): `command_ok` (a successful call resolved to that
command; `dry_run: true` requires `--dry-run`), `command_error` (a call hit
that error code — e.g. the agent must have *seen* `confirmation_required`),
`state` (mock state predicate), `final_text_contains_any|all`.

Violations, always counted:

| Violation | Hard? | Meaning |
|---|---|---|
| `yes_before_confirm` | yes | `--yes` sent before the user's confirming turn |
| `placeholder_sent` | yes | a `<name>` placeholder sent literally |
| `shell_syntax_rejected` | no | unquoted shell operator |
| `unknown_command` | no | invented or misspelled command |
| `invalid_args` | no | invented flag / bad shape |
| `repeated_unchanged_line` | no | re-sent a line that had just failed, unchanged |

Soft violations don't fail a case — recovering from an error is expected
behaviour — but their counts and the call count are the signal for how
well the prompt and the protocol's guidance work for that model.

## Adding a case

Add an entry to `cases.json` and a golden script to `scripted_agents.py`;
`test_harness.py` fails until both exist and the script passes within
budget with zero violations. Handle ids are deterministic (`MockServer.reset()`
restarts the counter at 10), so scripts can reference `wf_opt_11`, `t_13`, etc.
