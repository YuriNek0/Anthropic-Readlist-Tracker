# AGENTS.md

## Setup And Commands

- Python `>=3.12`; install in a UV env: `uv venv && uv pip install -e .`
- Primary entrypoints: `python -m anthropic_readings` and `anthropic-readings-daemon`
- Root `daemon.py` is only a wrapper that prepends `src/`; package `src/anthropic_readings/daemon.py` is the compatibility export surface used by tests.
- Fast config validation: `python -m anthropic_readings --check --config config.yaml`
- One-shot run: `python -m anthropic_readings --once --config config.yaml`
- Focused tests: `python -m pytest tests/test_daemon.py`, `python -m pytest tests/test_graph.py`
- OneDrive test helper: `python run_onedrive_test.py`; live mode requires `--live --credentials <yaml>` and optionally `--keep-remote`

## Config And Runtime Gotchas

- Config lookup order when `--config` is omitted: `./config.yaml`, `src/anthropic_readings/config.yaml`, repo-root `config.yaml`.
- Missing or invalid config exits immediately in `cli.py`; `Config.from_defaults()` is test support, not CLI fallback behavior.
- `email.sender` and `email.recipients` are required by config loading; do not rely on older behavior that allowed render-only runs without email config.
- `repos[].manifest_file` switches discovery into cookbook-manifest mode; otherwise discovery is glob-based course scanning from `discover_patterns`.
- `daemon.random_delay_max_hours >= 0`; `daemon.render_concurrency` and `daemon.upload_concurrency >= 1` are enforced during config load.

## Code Paths That Matter

- Main orchestration is in `src/anthropic_readings/orchestrator.py`; CLI only loads config and decides `--check`, `--once`, or scheduled mode.
- Repo sync is simple: `repository.ensure_repo_available()` does `git pull origin HEAD` if the folder exists, otherwise shallow `git clone --depth 1`.
- Cookbook discovery reads a YAML list from the configured manifest file; course discovery uses `Path.rglob()` and skips any path containing `AmazonBedrock`.
- Change detection is date plus content hash; unchanged docs are skipped before rendering.
- Rendering uses temp copies, rewrites internal `.md`/`.ipynb` links to relative `.pdf` paths, and strips notebook `metadata.widgets` before `nbconvert`.
- Markdown rendering is `pandoc ... --pdf-engine=weasyprint`; notebooks use `jupyter nbconvert --to=webpdf --allow-chromium-download`.

## Output And OneDrive Semantics

- Local outputs are temporary; `run_daemon()` cleans repo clones and output dirs in `finally`, even on failure.
- PDFs and version files are always uploaded under a repo-scoped path: PDFs under `<repo_name>/...`, version files under `<repo_name>/<basename(version_file)>`.
- Course output paths slugify every folder segment; OneDrive upload paths for `courses` also drop any `Anthropic 1P` path segment.
- Cookbook filenames come from the manifest title slug; a two-digit source prefix like `01_intro.md` becomes `01-<slug>.pdf`.
- Upload/link/version failures trigger rollback of already uploaded items for that run; rollback uses permanent delete by item id.
- Share links use explicit `email.share_recipients` when present, otherwise `email.recipients`; optional domain filtering is enforced in `graph.py`.

## Verification Notes

- Tests are `unittest` suites run comfortably via `pytest`; there is no repo-local lint/typecheck/CI config to consult here.
- If you touch Graph behavior, prefer `tests/test_graph.py` plus `tests/test_onedrive_daemon.py`; reserve live OneDrive tests for changes that need real Graph/AppFolder validation.
