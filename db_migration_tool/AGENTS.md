# Repository Guidelines

## Project Structure & Module Organization
- Application code lives in `src/` (`core/` for migration logic, `ui/` for Qt dialogs, `database/` for persistence, `models/` for DTOs).
- Tests are in `tests/` (markers: `unit`, `integration`); fixtures and helpers alongside.
- Assets and packaged resources are in `assets/` and `resources/`; build scripts in repo root (`Makefile`, `build_mac.sh`, `db_migration_tool.spec`).
- Use `migrations/` for local DB schema and `docs/` for design/usage notes.

## Build, Test, and Development Commands
- Install dev deps: `make install-dev` (uv-based) or `uv sync --all-extras`.
- Run app in dev: `make dev` or `uv run python src/main.py`.
- Tests: `make test` (verbose), `make test-unit`, `make test-integration`, coverage via `make test-cov`.
- Quality gates: `make check` (format + lint + typecheck), or individually `make format`, `make lint`, `make typecheck`.
- Package: `make build` (PyInstaller), `make build-mac` for macOS app bundle.

## Coding Style & Naming Conventions
- Python 3.9+; keep code formatted with `ruff format` and lint-clean with `ruff check`.
- Type hints are expected (mypy runs in CI); prefer explicit `Optional`/`| None` for nullable values.
- Use snake_case for variables/functions, PascalCase for classes, UPPER_SNAKE for constants.
- Keep UI strings and log messages concise; avoid non-ASCII unless already present.

## Testing Guidelines
- Prefer `pytest` with markers (`@pytest.mark.unit` / `integration`); name tests `test_*`.
- When touching data pipelines (COPY/INSERT, checkpoints), add coverage for resume paths and error branches.
- Run at least `make test` before submitting; add `--cov` when changing critical data paths.

## Commit & Pull Request Guidelines
- Commit messages: short imperative summary (e.g., “Add streaming COPY buffer”), optional body for rationale.
- Keep PRs focused; include what/why, user impact, and a short test plan (`make test`, manual steps).
- Link to related issues/tickets and add screenshots for UI changes (dialogs, progress bars, tray alerts).
- Note migration-impacting changes (DB schema, checkpoint format, resume logic) explicitly in the PR description.

## Memory
- 한국어로 답변해주세요.