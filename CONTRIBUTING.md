# Contributing to AAI

AAI is a draft protocol. Contributions that make the specification clearer,
more interoperable, or easier for agents to use are welcome.

## Before proposing a change

1. Read [`SPEC.md`](SPEC.md), especially the design principles in section 2,
   the conformance checklist in section 10, and the open questions in section
   12.
2. Search existing issues and pull requests to avoid duplicating an active
   discussion.
3. For a substantial or incompatible protocol change, open an issue first.
   Describe the agent task that motivates it and include concrete request and
   response examples.

## Making a change

- Use the normative terms `MUST`, `SHOULD`, and `MAY` consistently.
- Keep the protocol language- and transport-independent.
- Preserve progressive disclosure and a constant-size bootstrap.
- Update examples, JSON schemas, the conformance checklist, and
  [`AGENT_PROMPT.md`](AGENT_PROMPT.md) when the change affects them.
- Add or update an evaluation case when the change affects agent behaviour.
- Add user-visible changes under `Unreleased` in
  [`CHANGELOG.md`](CHANGELOG.md).

## Running the checks

The built-in checks require Python 3.10 or newer and no third-party packages:

```sh
python -m unittest tests/test_harness.py
python tests/llm_eval.py --provider scripted
```

To evaluate a live model, install its optional SDK and follow
[`tests/README.md`](tests/README.md). Do not commit API keys or generated files
from `tests/results/`.

## Pull requests

Keep pull requests focused. In the description, explain the interoperability
or agent-usage problem, summarize the chosen rule, and list the checks you ran.
By submitting a contribution, you agree that it is licensed under the MIT
License used by this repository.
