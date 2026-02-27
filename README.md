# gohook

Gmail Pub/Sub daemon — watches a Gmail account via Google Cloud Pub/Sub and fires configurable webhooks when labels change (e.g. starring an email).

## How it works

1. Registers a Gmail watch on your account pointing at a GCP Pub/Sub topic
2. Polls the subscription in a loop (pull mode, no public endpoint needed)
3. On each notification, fetches the Gmail History API to find label changes
4. Matches changes against configured triggers
5. Renders a payload template and POSTs to your webhook URL

Auth: gohook manages its own OAuth2 token (Gmail + Pub/Sub scopes). Run `python gohook.py --auth` once to authorize.

---

## Prerequisites

- Python 3.10+
- `gog` CLI credentials file (client_id + client_secret) — or any Google OAuth2 client credentials JSON
- A GCP project with Pub/Sub API enabled
- The Gmail API enabled in the same (or any) GCP project

---

## GCP setup

### 1. Create the Pub/Sub topic

```bash
gcloud pubsub topics create gmail-hook --project=YOUR_PROJECT_ID
```

### 2. Grant Gmail publish permission

Gmail needs permission to publish to your topic. Grant the service account:

```bash
gcloud pubsub topics add-iam-policy-binding gmail-hook \
  --project=YOUR_PROJECT_ID \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

### 3. Create a pull subscription

```bash
gcloud pubsub subscriptions create gmail-hook-sub \
  --topic=gmail-hook \
  --project=YOUR_PROJECT_ID \
  --ack-deadline=60 \
  --message-retention-duration=1d
```

### 4. Enable APIs

```bash
gcloud services enable gmail.googleapis.com pubsub.googleapis.com \
  --project=YOUR_PROJECT_ID
```

---

## Installation

```bash
git clone https://github.com/jodok/gohook
cd gohook
pip install -r requirements.txt
cp config.yaml.example config.yaml
# edit config.yaml with your project_id, topic, subscription, and triggers

# Step 1: authorize gohook (opens browser, saves token to ~/.gohook_token.json)
python gohook.py --auth
```

---

## Configuration

Edit `config.yaml`:

```yaml
account: you@example.com

pubsub:
  project_id: your-gcp-project-id
  topic: projects/your-gcp-project-id/topics/gmail-hook
  subscription: projects/your-gcp-project-id/subscriptions/gmail-hook-sub

triggers:
  - name: yellow_star
    condition:
      labels_added: ["STARRED"]
    webhook:
      url: https://your-service.com/webhook
      method: POST
      headers:
        Authorization: "Bearer YOUR_TOKEN"
      payload_template: |
        {
          "event": "yellow_star",
          "message_id": "{{message_id}}",
          "subject": "{{subject}}",
          "from": "{{from}}",
          "snippet": "{{snippet}}"
        }

watch:
  renew_interval_hours: 168
```

### Template variables

| Variable | Description |
|---|---|
| `{{message_id}}` | Gmail message ID |
| `{{thread_id}}` | Gmail thread ID |
| `{{subject}}` | Message subject |
| `{{from}}` | From header |
| `{{to}}` | To header |
| `{{snippet}}` | Message snippet (short preview) |
| `{{body}}` | Full plain-text email body (up to 4000 chars) |
| `{{labels}}` | Comma-separated current labels |

### Label IDs

Common Gmail label IDs:
- `STARRED` - starred / yellow star
- `INBOX` - inbox
- `UNREAD` - unread
- `IMPORTANT` - important
- `TRASH` - trash
- `SPAM` - spam

Custom labels use IDs like `Label_1234567890`. Find yours with:

```bash
gog gmail labels list --account you@example.com
```

---

## Running

```bash
# run with default config.yaml
python gohook.py

# run with a specific config
python gohook.py --config /path/to/config.yaml

# debug logging
python gohook.py --debug
```

The daemon logs to stdout with timestamps. Run it under systemd, supervisor, or a tmux session.

### systemd example

```ini
[Unit]
Description=gohook Gmail webhook daemon
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/gohook/gohook.py --config /opt/gohook/config.yaml
WorkingDirectory=/opt/gohook
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

---

## OpenClaw Integration

gohook is designed as a Gmail → [OpenClaw](https://openclaw.ai) notification daemon.

When a trigger fires, it POSTs to OpenClaw's agent webhook endpoint:

```
POST /hooks/agent
Authorization: Bearer <hooks_token>
```

The OpenClaw agent (e.g. Tashi) receives the full email content and decides what to do.

**Finding your hooks token:** open `openclaw.json` and look for `hooks.token`.

**Tailscale URL pattern:**
```
https://MACHINENAME.TAILNET.ts.net/hooks/agent
```

See the [OpenClaw webhook docs](https://docs.openclaw.ai/automation/webhook) for full details.

### Example trigger config

```yaml
triggers:
  - name: green_flag
    condition:
      labels_added: ["^sg"]   # green star label id
    webhook:
      url: https://yourhost.tailnet.ts.net/hooks/agent
      method: POST
      headers:
        Authorization: "Bearer YOUR_HOOKS_TOKEN"
      payload_template: |
        {
          "message": "Green-flagged email — handle this:\nFrom: {{from}}\nSubject: {{subject}}\n\n{{body}}",
          "name": "Gmail",
          "agentId": "main"
        }
```

---

## State file

gohook saves its last known Gmail `historyId` to `~/.gohook_state.json`. Delete it to reset.

---

## Troubleshooting

**Token file missing** — run the auth flow first:
```bash
python gohook.py --auth
```

**Token expired / invalid** — re-run the auth flow:
```bash
python gohook.py --auth
```

**No notifications received** — verify the Pub/Sub subscription exists and the Gmail watch is active. The watch auto-renews every `renew_interval_hours`.

**Webhook not firing** — run with `--debug` to see history events and trigger matching.

**historyId too old** — if the daemon was stopped for more than a few days, Gmail may reject the old historyId. Delete `~/.gohook_state.json` and restart.
