# Starting Company OS

## Prerequisites

- Python 3.11+ installed
- All dependencies installed: `pip install -r requirements.txt`
- (Optional) API keys in `.env`:
  - `MISTRAL_API_KEY` for real proposals via Mistral
  - `SERPER_API_KEY` for web research in proposals
  - `ANTHROPIC_API_KEY` for using Claude models

## Quick Start (Windows)

Double-click `startup.bat` in the repo root. It will:
1. Activate the Python environment (if in a venv)
2. Start the Company OS server on `http://127.0.0.1:8000`
3. Open the CEO console in your browser
4. Keep the terminal open (Ctrl+C to stop)

## Quick Start (macOS/Linux)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server
uvicorn app.main:app --reload

# 3. Open in your browser
open http://127.0.0.1:8000/console
```

The server runs in reload mode — changes to `.py` files auto-restart it.

## Manual Server Start (any OS)

```bash
uvicorn app.main:app --reload
```

or without reload (production-like):

```bash
uvicorn app.main:app
```

The server will:
- Initialize the SQLite database at `company_os.db` (auto-created)
- Run idempotent migrations on startup
- Listen on `http://127.0.0.1:8000`
- Log to the terminal

## Access Points

- **CEO Console** — `http://127.0.0.1:8000/console` — the web UI for humans
- **API** — `http://127.0.0.1:8000/docs` — interactive Swagger API documentation
- **LeadForge Webhook** — `POST /integrations/leadforge/proposal` — where LeadForge sends warm prospects

## Environment Variables

Create a `.env` file in the repo root (optional; defaults shown below):

```
# Database
DATABASE_URL=sqlite:///./company_os.db

# Auth
JWT_SECRET=dev-secret-change-me-in-prod-0123456789abcdef
JWT_TTL_SECONDS=43200

# LLM Providers (optional)
ANTHROPIC_API_KEY=sk-...
MISTRAL_API_KEY=...
SERPER_API_KEY=...

# Model URLs (optional; defaults shown)
OLLAMA_BASE_URL=http://localhost:11434
MISTRAL_BASE_URL=https://api.mistral.ai
SERPER_BASE_URL=https://google.serper.dev
```

## Running a Demo

After the server starts:

### 1. Create an org + CEO account

```bash
curl -X POST http://127.0.0.1:8000/orgs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Acme Corp",
    "ceo_email": "ceo@acme.com",
    "ceo_password": "testpass123"
  }'
```

Response includes `access_token` and `ceo_id`. Save the token.

### 2. Log in via the console

- Open `http://127.0.0.1:8000/console`
- Enter `ceo@acme.com` + `testpass123`
- You'll see the CEO dashboard

### 3. Run a real deal rehearsal

In another terminal (server still running in the first):

```bash
# Windows
set COMPANY_OS_EMAIL=ceo@acme.com
set COMPANY_OS_PASSWORD=testpass123
python -m scripts.run_one_deal

# macOS/Linux
COMPANY_OS_EMAIL=ceo@acme.com COMPANY_OS_PASSWORD=testpass123 python -m scripts.run_one_deal
```

This drives the full proposal loop: async generate → poll → approve → accept → done. It will:
1. Kick off a proposal generation for a Dubai med-spa
2. Poll while the background worker generates it (using your MISTRAL_API_KEY or local Ollama)
3. Have you approve it (auto-approved for demo)
4. Release the send-ready text
5. Print the actual proposal content

## Stopping the Server

Press `Ctrl+C` in the terminal where the server runs. The database is auto-saved.

## Troubleshooting

### Port already in use

If `8000` is already in use, run:

```bash
uvicorn app.main:app --port 8001 --reload
```

Then visit `http://127.0.0.1:8001/console`.

### No module named 'app'

Make sure you're running from the repo root:

```bash
cd C:\Pranith\Personal Projects\08-02-2026-AI-OS-PRANITH
uvicorn app.main:app --reload
```

### Database locked / "database is locked"

This happens under high concurrency. The database auto-recovers after ~30 seconds. SQLite is only for development; production should use Postgres.

### Proposal generation fails / "no provider configured"

Check:
1. An `AgentProfile` with a provider exists in your org (created on first org bootstrap)
2. The provider is reachable:
   - For Mistral: `MISTRAL_API_KEY` is set in `.env`
   - For Ollama: `ollama serve` is running on `http://localhost:11434`
   - For Anthropic: `ANTHROPIC_API_KEY` is set in `.env`

### Tests pass but rehearsal hangs on [2/5]

The model is generating. It can take 30–120 seconds depending on the provider. Let it finish.

## Next Steps

1. **Wire LeadForge** — Update the LeadForge repo to call the 3-step contract:
   - `POST /integrations/leadforge/proposal` to kick off
   - `GET /proposals/{id}` to poll while generating
   - After a human approves in Company OS, text is released and LeadForge sends it

2. **Run your first real deal** — A live prospect, a real proposal, a real signature — see if it converts.

3. **Customize agents** — Update department profiles and playbooks to match your actual business.

For more, see [docs/STATUS.md](docs/STATUS.md).
