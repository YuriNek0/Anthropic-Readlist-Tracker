# Anthropic Readings Manager

An automated daemon that monitors Anthropic's public GitHub repositories for new or updated educational content (cookbooks and courses), renders them to PDF, and sends email notifications with the PDFs as attachments.

## Features

- **GitHub Monitoring**: Tracks `claude-cookbooks` and `claude-courses` repositories
- **Change Detection**: Identifies new and updated documents using date comparison and content hashing
- **PDF Rendering**: Converts Jupyter notebooks (`.ipynb`) and Markdown (`.md`) to styled PDFs
- **Email Notifications**: Sends HTML emails with PDF attachments via Microsoft Graph API
- **Systemd Integration**: Ships as a systemd service + timer for daily scheduled runs
- **Course Organization**: Groups course materials into ZIP archives by folder

## Prerequisites

- Python 3.12+
- [UV](https://github.com/astral-sh/uv) package manager
- Pandoc + weasyprint (for Markdown-to-PDF)
- Chromium (for notebook-to-PDF conversion via nbconvert)
- Git

## Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd claude-cookbooks-auto-update

# Create virtual environment
uv venv
source .venv/bin/activate
uv pip install -e .
```

### System Dependencies

**macOS:**
```bash
brew install pandoc weasyprint
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install pandoc weasyprint
```

Chromium will be downloaded automatically by nbconvert.

## Configuration

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your settings:

| Section | Description |
|---------|-------------|
| `azure` | Azure AD tenant/client IDs for Microsoft Graph API |
| `user` | Email for device code flow authentication |
| `email` | Sender/recipient addresses |
| `repos` | GitHub repositories to track |
| `daemon` | Log level and random delay settings |

## Usage

### One-time Mode (testing)

```bash
python daemon.py --once --config config.yaml
```

### Check Configuration

```bash
python daemon.py --check --config config.yaml
```

### Systemd Service (production)

```bash
# Copy service files
sudo cp anthropic_daemon.service /etc/systemd/system/
sudo cp anthropic_daemon.timer /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable anthropic_daemon.timer
sudo systemctl start anthropic_daemon.timer
```

## Project Structure

```
.
├── daemon.py                 # Main daemon implementation
├── test_daemon.py           # Unit tests
├── pyproject.toml            # Python project config
├── config.yaml.example       # Example configuration template
├── anthropic_daemon.service  # Systemd oneshot service
├── anthropic_daemon.timer    # Systemd timer (daily run)
└── README.md                 # This file
```

## How It Works

1. **Sync**: Clones/pulls from configured GitHub repositories
2. **Detect**: Compares documents against previously tracked versions
3. **Render**: Converts changed notebooks and markdown to PDF
4. **Notify**: Sends email with PDFs attached (individual docs or zipped by course folder)
