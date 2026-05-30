# Anthropic Readings Daemon

Anthropic Readings Daemon is a Python service that watches Anthropic learning repositories, renders changed documents to PDF, uploads the outputs to Microsoft OneDrive AppFolder, and emails recipients links to the latest documents.

> Disclaimer: This project was initially assembled with AI assistance to deliver core functionality quickly, and has not yet been fully polished for broad public consumption.

It is designed to run as an unattended background job, with a safe one-shot mode for local testing and manual runs.

## What it does

- Keeps a local clone of configured GitHub repositories and updates them on each run.
- Discovers documents from:
  - `cookbooks`: from `registry.yaml` entries
  - `courses`: from configurable file globs (for example `*.md`, `*.ipynb`)
- Detects changes using both document date and file content hash (SHA-256, truncated).
- Renders new/updated markdown and notebooks to PDF:
  - Markdown (`.md`) via `pandoc` + `weasyprint`
  - Jupyter Notebooks (`.ipynb`) via `jupyter nbconvert --to=html` + `weasyprint`
- Rewrites internal links before rendering so references to `.md`/`.ipynb` become `.pdf` links.
- Uploads generated PDFs and per-repo version metadata files to OneDrive under `special/approot` (AppFolder).
- Generates recipient-scoped sharing links for each uploaded PDF.
- Sends a single update email with all new/updated links and render/upload errors.
- Performs all upload/link/version operations with rollback behavior: if any stage fails, uploaded OneDrive items are removed.
- Cleans up temporary output and cloned repository directories after each run.

## Functional behavior that matters in practice

- **No config fallback**: startup fails fast if config is missing or invalid.
- **Date stability during a run**: output folders use the discovered document metadata date (not file mtime), preventing date drift on long runs.
- **Course paths**:
  - `AmazonBedrock` content is excluded.
  - Folder structure is preserved in output/upload paths (except `Anthropic 1P` path segments are dropped in OneDrive PDFs).
- **Cookbook naming**:
  - PDF filenames come from manifest titles and are slugified.
  - `00-foo.md` becomes `00-<slugified-title>.pdf`.
- **Upload paths** are repo-scoped:
  - PDFs: `<repo_name>/...`
  - Version files: `<repo_name>/<version_file_name>`
- **OneDrive cleanup semantics**:
  - rollback deletes uploaded artifacts via permanent delete endpoint behavior where required.
- **Error reporting**: if configured as production, failures are also summarized and sent as an error email.

## Repository structure

```text
.
├── src/
│   └── anthropic_readings/
│       ├── core/
│       │   ├── link_rewrite.py
│       │   └── output_paths.py
│       ├── cli.py
│       ├── config.py
│       ├── discovery.py
│       ├── orchestrator.py
│       ├── rendering.py
│       ├── graph.py
│       ├── mailer.py
│       ├── repository.py
│       └── models.py
├── daemon.py
├── run_onedrive_test.py
├── anthropic_daemon.service
├── anthropic_daemon.timer
├── config.yaml.example
└── tests/
```

## Prerequisites

- Python 3.12+
- `uv`
- `git`
- `pandoc`
- `weasyprint`
- Microsoft Entra / Azure AD app with Microsoft Graph access

### Install system packages

macOS:

```bash
brew install pandoc weasyprint
```

Debian / Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y pandoc weasyprint git
```

Notebook rendering uses static HTML plus `weasyprint`, so Chromium is not required.

## Installation

### 1) Clone

```bash
git clone <your-repo-url>
cd claude-cookbooks-auto-update
```

### 2) Create a virtual environment and install the package

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

The project entry point is:

```bash
anthropic-readings-daemon --help
```

## Nix flake

This repository now includes a `flake.nix` that exposes:

- `packages.<system>.default`: the packaged `anthropic-readings-daemon`
- `homeManagerModules.default`: a Home Manager module for a `systemd --user` service
- `nixosModules.default`: a NixOS module that also defines a `systemd --user` service

The packaged daemon is wrapped with the runtime tools it needs at execution time:

- `git`
- `pandoc`
- `weasyprint`

### Home Manager example

```nix
{
  inputs.anthropic-readings.url = "path:/path/to/claude-cookbooks-auto-update";

  outputs = { self, nixpkgs, home-manager, anthropic-readings, ... }: {
    homeConfigurations.alice = home-manager.lib.homeManagerConfiguration {
      pkgs = import nixpkgs { system = "x86_64-linux"; };
      modules = [
        anthropic-readings.homeManagerModules.default
        {
          services.anthropic-readings = {
            enable = true;
            configFile = "/home/alice/.config/anthropic-readings/config.yaml";
          };
        }
      ];
    };
  };
}
```

### NixOS example

```nix
{
  inputs.anthropic-readings.url = "path:/path/to/claude-cookbooks-auto-update";

  outputs = { self, nixpkgs, anthropic-readings, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        anthropic-readings.nixosModules.default
        {
          services.anthropic-readings = {
            enable = true;
            configFile = "/home/alice/.config/anthropic-readings/config.yaml";
          };
        }
      ];
    };
  };
}
```

The user service runs continuously with `Restart=always` and `RestartSec=4h` by default, so if it exits, systemd waits four hours before attempting the next restart. You can override that with `services.anthropic-readings.restartDelay`.

You can also run with Python directly:

```bash
python -m anthropic_readings --help
```

### 3) Provide configuration

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` to match your environment.

## Configuration

### Required sections

- `azure.tenant_id`
- `azure.client_id`
- `azure.client_secret`
- `user.email`
- `email.sender`
- `email.recipients` (non-empty list)
- `repos` (at least one repo config)

### Required repo fields

Each repo item requires:

- `name`
- `url`
- `local_path`
- `version_file`
- `discover_patterns`

Optional repo field:

- `manifest_file` (used for cookbook manifest mode)

### Example configuration

```yaml
azure:
  tenant_id: "your-tenant-id"
  client_id: "your-client-id"
  client_secret: "your-client-secret"

user:
  email: "operator@company.com"
  # Leave empty to use device-code flow (recommended for unattended jobs)
  password: ""

email:
  sender: "operator@company.com"
  sender_name: "Anthropic Readings"
  recipients:
    - "recipient1@company.com"
    - "recipient2@company.com"
  # Optional: explicit users for sharing links
  # share_recipients:
  #   - "reader1@company.com"
  share_domain_filter_enabled: false
  share_domain: "company.com"
  subject_prefix: "[Anthropic Readings]"

paths:
  output_dir: "outputs"

repos:
  - name: "cookbooks"
    url: "https://github.com/anthropics/claude-cookbooks.git"
    local_path: "claude-cookbooks"
    version_file: "cookbook-version.json"
    discover_patterns:
      - "*.ipynb"
    manifest_file: "registry.yaml"

  - name: "courses"
    url: "https://github.com/anthropics/courses.git"
    local_path: "claude-courses"
    version_file: "courses-version.json"
    discover_patterns:
      - "*.ipynb"
      - "*.md"
    manifest_file: null

daemon:
  log_level: "INFO"
  random_delay_max_hours: 1
  render_concurrency: 1
  render_timeout_seconds: 600
  upload_concurrency: 4

is_production: false
```

### Authentication

- **Device code flow** (recommended): set `user.password: ""`.
- **Username/password flow**: provide both `user.email` and `user.password`.

### Graph API scopes

The daemon asks for graph scopes similar to:

- `User.Read`
- `Mail.Send`
- `Files.ReadWrite` (AppFolder workflow expects AppFolder access)

## CLI usage

Validate config:

```bash
python -m anthropic_readings --check --config config.yaml
```

Run once (recommended for testing):

```bash
python -m anthropic_readings --once --config config.yaml
```

Run continuously:

```bash
python -m anthropic_readings --config config.yaml
```

If `--config` is omitted, defaults are checked in this order:

1. `./config.yaml`
2. `src/anthropic_readings/config.yaml`
3. `<project-root>/config.yaml`

If `schedule` is not installed, long-lived mode automatically falls back to `--once`.

## Production deployment (systemd)

The included unit files are wired for installation at `/opt/anthropic-readings/`.

```bash
sudo cp anthropic_daemon.service /etc/systemd/system/
sudo cp anthropic_daemon.timer /etc/systemd/system/
sudo systemctl enable anthropic_daemon.timer
sudo systemctl start anthropic_daemon.timer
```

Service checks:

```bash
systemctl status anthropic_daemon.timer
systemctl status anthropic_daemon.service
journalctl -u anthropic-daemon -n 200
```

## Run lifecycle (what happens in one run)

1. Load and validate config
2. Clone or pull each configured repo
3. Discover current documents
4. Compare against previous version metadata in OneDrive
5. Render only changed documents
6. Upload PDFs and create sharing links
7. Send one consolidated email update
8. Upload updated version metadata files
9. Remove temporary local artifacts on completion

## Notes for troubleshooting

- If startup fails with `CONFIG ERROR`, fix the reported field and rerun `--check`.
- If PDF output fails, verify `pandoc`, `weasyprint`, and notebook rendering dependencies.
- Keep `daemon.render_concurrency: 1` on low-memory hosts; each render can be memory-intensive.
- If emails are skipped, verify `Mail.Send` consent in Azure and that `email.sender` / `email.recipients` are valid.
- If device code auth is expected, keep `user.password` empty.
- Local output and cloned repo directories are deleted after the run; if a repo disappears unexpectedly, it will be re-cloned next run.

## Testing

Targeted unit suite:

```bash
python -m pytest tests/test_daemon.py
```

Run all tests:

```bash
python -m pytest tests
```

OneDrive-focused tests (mocked by default):

```bash
python run_onedrive_test.py
```

Live OneDrive test:

```bash
python run_onedrive_test.py --live --credentials /path/to/onedrive-test-credentials.yaml
```

Keep remote test artifacts:

```bash
python run_onedrive_test.py --live --credentials /path/to/onedrive-test-credentials.yaml --keep-remote
```

## Why use this daemon

Use this project when you need a reliable pipeline that continuously keeps OneDrive links updated as Anthropic content evolves, while keeping link references valid inside rendered PDFs and preserving repo-aware upload locations.
