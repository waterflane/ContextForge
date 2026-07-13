# Development Guide

## Requirements

- Python 3.12 or newer.
- A POSIX shell, PowerShell, or another shell capable of running Python tools.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Common commands

```bash
contextforge version
contextforge doctor
ruff check .
ruff format --check .
mypy
pytest
```

## Local API

```bash
uvicorn contextforge.api.app:create_app --factory
```

Then check:

- `GET /health`
- `GET /version`
