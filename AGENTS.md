# AGENTS.md — gohook

gohook is a Gmail -> OpenClaw notification daemon. It is NOT a generic webhook proxy.

> **ALWAYS work inside `.venv`** — see [Development](#development) below.

## Purpose
Subscribe to Gmail via Google Cloud Pub/Sub. When a configured Gmail event occurs (e.g. green flag / label change), notify OpenClaw directly via its webhook endpoint.

## Architecture
- Daemon: `gohook.py`
- Auth: gohook manages its own OAuth2 token (Gmail readonly + Pub/Sub scopes). Token stored at `~/.gohook_token.json` (configurable via `oauth.token_file` in config.yaml). Run `python gohook.py --auth` once to authorize via browser flow.
- Trigger: configurable label changes in `config.yaml`
- Destination: always OpenClaw `/hooks/agent` endpoint (hardcoded target)

## Auth flow
1. **First run:** `python gohook.py --auth` - opens browser, saves token to `~/.gohook_token.json`
2. **Normal run:** token loaded from file, auto-refreshed when expired
3. **Client credentials:** read from gog's credentials file (`credentials-{gog_client}.json`) or configured via `oauth.credentials_file` in config.yaml
4. **Scopes:** `gmail.readonly` + `pubsub`

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
- `~/.gohook_token.json` — OAuth token (created by `--auth`)

### `gog_client` field
Set `gog_client` in `config.yaml` to the gog client name that matches your Gmail account.
Used to locate the credentials file at `~/Library/Application Support/gogcli/credentials-{gog_client}.json`.
Override with `oauth.credentials_file` if needed.

```yaml
# Recommended: set explicitly
gog_client: myclient
```

## Key design decisions
- Full email body is passed to OpenClaw (not just message ID) — avoids extra round-trips and gives the agent immediate context to act
- Pull-based Pub/Sub (not push) — no public endpoint needed
- gohook owns its own OAuth token with both required scopes (gmail.readonly + pubsub) — previously relied on gog's token which only had gmail scopes, causing 403 on Pub/Sub calls

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
