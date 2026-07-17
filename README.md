# Interface V2

[简体中文](README.zh-CN.md)

Interface V2 is an experiment platform for studying trust in AI systems. It
combines a FastAPI backend, SQLite persistence, React participant and
administrator interfaces, controlled AI conversation flows, speech recognition,
data export tools, and deployment templates.

## Repository scope

This repository contains application source, tests, dependency locks, safe
configuration examples, and portable operations documentation. It intentionally
does not contain participant records, audio, exports, logs, credentials, local
environments, generated frontend assets, or internal design/planning materials.

## Requirements

- Python 3.11 or later
- Node.js 20.19 or later, or Node.js 22.12 or later
- npm compatible with the selected Node.js release
- `uv` is recommended for Python dependency management

## Local setup

From the repository root, install the Python and frontend dependencies:

```bash
uv sync --extra dev
cd frontend && npm ci
```

If `uv` is unavailable, use:

```bash
python3 -m pip install -e ".[dev]"
```

Create a local environment file from the provided template when testing provider,
ASR, or administrator functionality. Keep all filled environment files outside
Git. At a minimum, the backend needs a non-empty `APP_SECRET_KEY`.

## Development

Run the backend from the repository root:

```bash
APP_SECRET_KEY=local-development-only \
uv run python -m uvicorn backend.app.main:app --reload --port 8000 --no-proxy-headers
```

In a second terminal, run the frontend:

```bash
cd frontend && npm run dev
```

The development script starts both processes for convenience:

```bash
scripts/run_dev.sh
```

For a production-style local check, build the frontend and use the backend to
serve it:

```bash
cd frontend && npm run build
cd ..
APP_SECRET_KEY=local-development-only \
uv run python -m uvicorn backend.app.main:app --port 8000 --no-proxy-headers
```

The generated `frontend/dist/` directory is intentionally ignored.

## Configuration

`deployment/interface-v2.env.example` lists the operator-managed variables.
Copy it to a protected environment file and set values appropriate to the
deployment. Do not commit API keys, administrator credentials, session secrets,
or filled environment files.

Production requires administrator credentials and the provider/ASR credentials
used by the experiment. Review the following before enabling participant access:

- `APP_SECRET_KEY` changes invalidate existing sessions.
- `ADMIN_PASSWORD_HASH` should use an Argon2id hash.
- `APP_BASE_URL` must be the public HTTPS URL; microphone access requires HTTPS
  outside localhost.
- `DATA_DIR` and `DATABASE_URL` must point to a directory writable only by the
  service account.
- `TENCENT_ASR_ENDPOINT` must match the Tencent Cloud ASR region configured for
  the supplied credentials.

Generate the Argon2id hash without adding the plaintext password to shell
history:

```bash
uv run python -c 'from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass("Admin password: ")))'
```

## Verification

Run focused backend checks:

```bash
uv run python -m pytest backend/tests/test_health.py backend/tests/test_static_serving.py -v
uv run python -m py_compile backend/app/main.py backend/app/settings.py
```

Run frontend checks:

```bash
cd frontend && npm run typecheck
cd frontend && npm run build
```

For the complete backend suite:

```bash
uv run python -m pytest backend/tests -v
```

## Documentation

- [Operations guide](docs/OPERATIONS.md): setup, verification, secure deployment,
  backup, and recovery procedures.
- [Data structure guide](docs/DATA_STRUCTURE.md): the principal experiment records,
  export boundaries, and data-handling rules.

## Security and data handling

Runtime data under `data/` can contain participant identity, experiment
responses, recordings, transcripts, and operational logs. It is excluded by
default; only empty `.gitkeep` markers are versioned. Before publishing a
change, review `git status`, confirm that generated files remain ignored, and
scan staged changes for credentials or participant material.
