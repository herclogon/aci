# Changelog

This file records notable changes to the ACI specification and its evaluation
harness. Because ACI is still a draft, incompatible changes may occur between
draft releases.

## [Unreleased]

### Added

- A first-class OpenRouter evaluation provider using `OPENROUTER_API_KEY`.
- A catalog-validated Qwen model-size sweep runner.

## [0.2] - 2026-09-21

### Added

- The `invoke` request and result envelopes, including the primary `line`
  form and alternative `argv` form.
- Hierarchical command discovery through `help`, `search`, and scenarios.
- Typed arguments, `@input` references, sessions, tasks, handles, pagination,
  permissions, confirmation, and idempotency rules.
- Transport bindings and JSON schemas.
- The compact host-supplied agent prompt.
- A mock ACI application and model-evaluation harness with golden scripts and
  self-tests.

[Unreleased]: https://github.com/herclogon/aci/compare/8ef1077...HEAD
[0.2]: https://github.com/herclogon/aci/commit/8ef1077
