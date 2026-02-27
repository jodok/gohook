# AGENTS.md — gohook

gohook is a Gmail → OpenClaw notification daemon. It is NOT a generic webhook proxy.

## Purpose
Subscribe to Gmail via Google Cloud Pub/Sub. When a configured Gmail event occurs (e.g. green flag / label change), notify OpenClaw directly via its webhook endpoint.

## Architecture
- Daemon: `gohook.py`
- Auth: reuses gog's OAuth2 tokens (no separate setup)
- Trigger: configurable label changes in `config.yaml`
- Destination: always OpenClaw `/hooks/agent` endpoint (hardcoded target)

## OpenClaw Webhook
Notifications go to OpenClaw's `/hooks/agent` endpoint (POST):
```json
{
  "message": "Green-flagged email — handle this:\nFrom: {from}\nSubject: {subject}\n\n{body}",
  "name": "Gmail",
  "agentId": "main"
}
```
Auth: `Authorization: Bearer <hooks_token>`

See: https://docs.openclaw.ai/automation/webhook

The agent (Tashi) receives the full email content and decides what to do with it.

## Config
- `config.yaml` — runtime config (not committed, based on `config.yaml.example`)
- `~/.gohook_state.json` — persisted historyId + watch expiry

## Key design decisions
- Full email body is passed to OpenClaw (not just message ID) — avoids extra round-trips and gives Tashi immediate context to act
- Pull-based Pub/Sub (not push) — no public endpoint needed
- Token auto-refresh via gog CLI

## When modifying
- Keep OpenClaw as the hardcoded target — this is not a generic proxy
- Use branch `tashi/` prefix for all PRs
- Never push directly to main
