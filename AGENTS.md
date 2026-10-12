# Repository Guidelines

## Project Structure & Module Organization

A Cargo workspace (edition 2024) with three members:

- `dogwood-local-engine/` — the primary crate (lib `dogwood_local_engine`): a durable, incremental temporal authorization engine. Source in `src/`, integration tests in `tests/`, regression fixtures in `tests/fixtures/`.
- `dogwood-server/` — a demo service putting the engine behind Unix sockets (`lib` plus `src/main.rs` CLI).
- `dogwood-performance/` — an unpublished benchmark harness; benches in `benches/`, load binary in `src/bin/`.

`dogwood-language`, the frontend, is an external git dependency pinned by `rev`.

## Build, Test, and Development Commands

- `cargo build --workspace --all-targets` — build every crate and test target (CI's gate).
- `cargo test --workspace` — run unit and integration tests.
- `cargo bench -p dogwood-performance --bench decide_latency` — criterion benchmarks; see `dogwood-performance/README.md`.
- `cargo run --release -p dogwood-performance --bin load -- --policies 10,100 --rates 1,10 --seconds 5` — open-loop load test.
- `cargo fmt` and `cargo clippy --workspace --all-targets` — format and lint.

`Cargo.lock` is gitignored, so dependencies resolve fresh on each build.

## Coding Style & Naming Conventions

- Use rustfmt defaults: 4-space indent, 100-column width, trailing commas.
- Standard Rust naming: `snake_case` for functions and modules, `UpperCamelCase` for types, `SCREAMING_SNAKE_CASE` for constants.
- This codebase is comment-heavy by design: `//!` and `///` comments explain *why* a mechanism exists, so preserve them when editing.
- Test names read as claims: `fn an_acked_offset_that_is_gone_is_caught()`.

## Testing Guidelines

- Unit tests sit beside the code under `#[cfg(test)]`; cross-crate behavior lives in `<crate>/tests/<topic>.rs`, with shared helpers in `tests/common/`.
- Prefer differential tests: verdicts must agree with the frontend's reference interpreter (`tests/corpus_diff.rs`, `tests/recovery_oracle.rs`).
- Give each test its own temporary store path rather than sharing durable state.
- The `shuttle` feature is for build and lint checks only — never execute tests with it enabled.

## Commit & Pull Request Guidelines

- History holds a single "Initial public release" commit, so there is no established message convention.
- This repository is a published, read-only mirror of an internal repository. External pull requests are not merged and GitHub issues are not used; see `CONTRIBUTING.md`.
- CI (`.github/workflows/ci.yml`) must build and test the workspace; a red sync PR cannot merge.

## Security & Configuration Tips

- Report vulnerabilities through the process in `SECURITY.md`, never in a public issue.
- The engine issues verdicts but does not enforce them or authenticate event provenance; the embedding application owns enforcement and store/policy access control.
