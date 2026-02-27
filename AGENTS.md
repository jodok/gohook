# AGENTS.md — gohook

gohook is a Gmail → OpenClaw notification daemon. It is NOT a generic webhook proxy.

> **ALWAYS work inside `.venv`** — see [Development](#development) below.

## Purpose
Subscribe to Gmail via Google Cloud Pub/Sub. When a configured Gmail event occurs (e.g. green flag / label change), notify OpenClaw directly via its webhook endpoint.

## Architecture
- Daemon: `gohook.py`
- Auth: reuses gog's OAuth2 tokens (no separate Google auth setup needed)
- Trigger: configurable label changes in `config.yaml`
- Destination: always OpenClaw `/hooks/agent` endpoint (hardcoded target)

## OpenClaw Webhook
Notifications go to OpenClaw's `/hooks/agent` endpoint (POST):
```json
{
  "message": "Green-flagged email — handle this:\nFrom: {from}\nSubject: {subject}\n\n{body}",
  "name": "Gmail",
  "agentId": "YOUR_AGENT_ID"
}
```
Auth: `Authorization: Bearer <hooks_token>`

See: https://docs.openclaw.ai/automation/webhook

The agent receives the full email content and decides what to do with it.

## Config
- `config.yaml` — runtime config (not committed, based on `config.yaml.example`)
- `~/.gohook_state.json` — persisted historyId + watch expiry

### `gog_client` field
Set `gog_client` in `config.yaml` to the gog client name that matches your Gmail account.
This is the identifier gog uses for its credential files (e.g. `credentials-myclient.json`).
If not set, gohook will attempt to derive it from the email domain, which may not be correct.

```yaml
# Recommended: set explicitly
gog_client: myclient
```

## Key design decisions
- Full email body is passed to OpenClaw (not just message ID) — avoids extra round-trips and gives the agent immediate context to act
- Pull-based Pub/Sub (not push) — no public endpoint needed
- Token auto-refresh via gog CLI

## Development

Always use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

All development and testing must happen inside `.venv`. Add `.venv/` to `.gitignore` if not already there.

## When modifying
- Keep OpenClaw as the hardcoded target — this is not a generic proxy
- Use branch `feature/` or `fix/` prefix for PRs
- Never push directly to main
