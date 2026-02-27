#!/usr/bin/env python3
"""
gohook - Gmail Pub/Sub daemon
Watches Gmail via Google Cloud Pub/Sub and fires webhooks on label changes.

Auth: gohook manages its own OAuth2 token with Gmail + Pub/Sub scopes.
Run `python gohook.py --auth` once to authorize.
"""

import json
import logging
import os
import signal
import sys
import time
from typing import Optional

import requests
import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# --- logging ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("gohook")

# --- state file ---------------------------------------------------------------

STATE_PATH = os.path.expanduser("~/.gohook_state.json")


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.warning("could not read state file: %s", e)
    return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error("could not write state file: %s", e)


# --- auth ---------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/pubsub",
]


def _credentials_file(cfg: dict) -> str:
    oauth_cfg = cfg.get("oauth", {})
    if oauth_cfg.get("credentials_file"):
        return os.path.expanduser(oauth_cfg["credentials_file"])
    client = cfg.get("gog_client") or cfg.get("account", "").split("@")[1].split(".")[0]
    return os.path.expanduser(
        f"~/Library/Application Support/gogcli/credentials-{client}.json"
    )


def _token_file(cfg: dict) -> str:
    oauth_cfg = cfg.get("oauth", {})
    if oauth_cfg.get("token_file"):
        return os.path.expanduser(oauth_cfg["token_file"])
    return os.path.expanduser("~/.gohook_token.json")


def run_auth_flow(cfg: dict) -> None:
    """Interactive OAuth flow - opens browser, saves token, then exits."""
    creds_file = _credentials_file(cfg)
    token_file = _token_file(cfg)

    if not os.path.exists(creds_file):
        print(f"ERROR: credentials file not found: {creds_file}")
        print("Set oauth.credentials_file in config.yaml or ensure the gog credentials file exists.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_file, "w") as f:
        f.write(creds.to_json())
    print(f"Token saved to {token_file}")
    print("You can now run gohook normally: python gohook.py")


def load_credentials(cfg: dict) -> Credentials:
    """Load credentials from token_file, refreshing if expired."""
    token_file = _token_file(cfg)

    if not os.path.exists(token_file):
        print(f"ERROR: token file not found: {token_file}")
        print("Run `python gohook.py --auth` once to authorize.")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("refreshing OAuth token")
            creds.refresh(Request())
            with open(token_file, "w") as f:
                f.write(creds.to_json())
        else:
            print("ERROR: token is invalid and cannot be refreshed.")
            print("Run `python gohook.py --auth` to re-authorize.")
            sys.exit(1)

    return creds


# --- config -------------------------------------------------------------------


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# --- Gmail / Pub/Sub helpers --------------------------------------------------


def gmail_watch(service, email: str, topic: str) -> dict:
    body = {
        "labelIds": ["INBOX"],
        "topicName": topic,
    }
    result = service.users().watch(userId=email, body=body).execute()
    log.info("gmail watch registered: historyId=%s expiration=%s",
             result.get("historyId"), result.get("expiration"))
    return result


def pubsub_pull(project_id: str, subscription: str, creds: Credentials,
                max_messages: int = 10) -> list:
    url = f"https://pubsub.googleapis.com/v1/{subscription}:pull"
    body = {"maxMessages": max_messages, "returnImmediately": False}
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    resp = requests.post(url, json=body, headers=headers, timeout=35)
    if resp.status_code == 401:
        raise TokenExpiredError("pubsub 401")
    resp.raise_for_status()
    return resp.json().get("receivedMessages", [])


def pubsub_ack(subscription: str, ack_ids: list, creds: Credentials) -> None:
    url = f"https://pubsub.googleapis.com/v1/{subscription}:acknowledge"
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    resp = requests.post(url, json={"ackIds": ack_ids}, headers=headers, timeout=15)
    if resp.status_code not in (200, 204):
        log.warning("ack failed: %s %s", resp.status_code, resp.text[:200])


def get_history(service, email: str, start_history_id: str) -> list:
    items = []
    page_token = None
    while True:
        kwargs = {
            "userId": email,
            "startHistoryId": start_history_id,
            "historyTypes": ["labelAdded", "labelRemoved"],
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            result = service.users().history().list(**kwargs).execute()
        except HttpError as e:
            if e.resp.status == 404:
                log.warning("historyId %s too old, resetting", start_history_id)
                return []
            raise
        items.extend(result.get("history", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def get_message(service, email: str, message_id: str) -> dict:
    return service.users().messages().get(
        userId=email, id=message_id, format="full",
    ).execute()


def extract_header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def extract_body(msg: dict, max_chars: int = 4000) -> str:
    import base64
    import html as html_mod
    import re as _re

    def _decode(data: str) -> str:
        padded = data + "=" * (4 - len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

    def _strip_html(raw: str) -> str:
        text = _re.sub(r"<[^>]+>", " ", raw)
        text = html_mod.unescape(text)
        return _re.sub(r"\s+", " ", text).strip()

    def _walk(part: dict) -> tuple:
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data", "")
        if mime == "text/plain" and body_data:
            return _decode(body_data), ""
        if mime == "text/html" and body_data:
            return "", _decode(body_data)
        plain, html = "", ""
        for sub in part.get("parts", []):
            p, h = _walk(sub)
            plain = plain or p
            html = html or h
        return plain, html

    payload = msg.get("payload", {})
    plain, html = _walk(payload)
    text = plain or (_strip_html(html) if html else msg.get("snippet", ""))
    return text[:max_chars]


# --- token management ---------------------------------------------------------


class TokenExpiredError(Exception):
    pass


class AuthManager:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._creds: Optional[Credentials] = None
        self._gmail_service = None

    def refresh(self) -> None:
        log.info("loading/refreshing OAuth credentials")
        self._creds = load_credentials(self._cfg)
        self._gmail_service = build("gmail", "v1", credentials=self._creds)

    @property
    def creds(self) -> Credentials:
        if self._creds is None or not self._creds.valid:
            self.refresh()
        return self._creds

    @property
    def gmail(self):
        if self._gmail_service is None:
            self.refresh()
        return self._gmail_service

    def handle_401(self) -> None:
        log.warning("got 401, refreshing token")
        self.refresh()


# --- trigger matching & webhook dispatch --------------------------------------


def labels_match(condition: dict, labels_added: list, labels_removed: list) -> bool:
    req_added = set(condition.get("labels_added", []))
    req_removed = set(condition.get("labels_removed", []))
    if req_added and not req_added.issubset(set(labels_added)):
        return False
    if req_removed and not req_removed.issubset(set(labels_removed)):
        return False
    return bool(req_added or req_removed)


def render_template(template: str, variables: dict) -> str:
    result = template
    for k, v in variables.items():
        result = result.replace("{{" + k + "}}", str(v))
    return result


def fire_webhook(trigger: dict, variables: dict) -> None:
    wh = trigger["webhook"]
    url = wh["url"]
    method = wh.get("method", "POST").upper()
    headers = dict(wh.get("headers", {}))
    template = wh.get("payload_template", "{}")
    payload_str = render_template(template, variables)
    try:
        payload = json.loads(payload_str)
        is_json = True
    except json.JSONDecodeError:
        payload = payload_str
        is_json = False

    if is_json and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    log.info("firing webhook '%s' -> %s %s", trigger["name"], method, url)
    try:
        if is_json:
            resp = requests.request(method, url, json=payload, headers=headers, timeout=15)
        else:
            resp = requests.request(method, url, data=payload, headers=headers, timeout=15)
        log.info("webhook '%s' response: %s", trigger["name"], resp.status_code)
        if not resp.ok:
            log.warning("webhook response body: %s", resp.text[:500])
    except Exception as e:
        log.error("webhook '%s' failed: %s", trigger["name"], e)


# --- notification processing --------------------------------------------------


def process_notification(auth: AuthManager, config: dict, notification: dict,
                          state: dict) -> None:
    email = notification.get("emailAddress", config["account"])
    new_history_id = notification.get("historyId")
    if not new_history_id:
        return

    current_history_id = state.get("history_id")
    if not current_history_id:
        log.info("no stored historyId, using notification historyId as base")
        state["history_id"] = new_history_id
        save_state(state)
        return

    log.info("fetching history from %s to %s", current_history_id, new_history_id)
    try:
        history_items = get_history(auth.gmail, email, current_history_id)
    except HttpError as e:
        if e.resp.status == 401:
            auth.handle_401()
            history_items = get_history(auth.gmail, email, current_history_id)
        else:
            raise

    for item in history_items:
        for change_type, label_key in [
            ("labelsAdded", "labelIds"),
            ("labelsRemoved", "labelIds"),
        ]:
            for change in item.get(change_type, []):
                changed_labels = change.get(label_key, [])
                msg_id = change["message"]["id"]
                added = changed_labels if change_type == "labelsAdded" else []
                removed = changed_labels if change_type == "labelsRemoved" else []

                for trigger in config.get("triggers", []):
                    cond = trigger.get("condition", {})
                    if labels_match(cond, added, removed):
                        log.info("trigger '%s' matched on message %s", trigger["name"], msg_id)
                        try:
                            msg = get_message(auth.gmail, email, msg_id)
                        except HttpError as e:
                            if e.resp.status == 401:
                                auth.handle_401()
                                msg = get_message(auth.gmail, email, msg_id)
                            else:
                                log.error("could not fetch message %s: %s", msg_id, e)
                                continue
                        variables = {
                            "message_id": msg_id,
                            "thread_id": msg.get("threadId", ""),
                            "subject": extract_header(msg, "Subject"),
                            "from": extract_header(msg, "From"),
                            "to": extract_header(msg, "To"),
                            "snippet": msg.get("snippet", ""),
                            "labels": ",".join(msg.get("labelIds", [])),
                            "body": extract_body(msg),
                        }
                        fire_webhook(trigger, variables)

    state["history_id"] = new_history_id
    save_state(state)


# --- watch renewal ------------------------------------------------------------


def maybe_renew_watch(auth: AuthManager, config: dict, state: dict) -> None:
    interval_hours = config.get("watch", {}).get("renew_interval_hours", 168)
    interval_sec = interval_hours * 3600
    last_watch = state.get("last_watch_at", 0)

    if time.time() - last_watch < interval_sec:
        return

    topic = config["pubsub"]["topic"]
    email = config["account"]
    try:
        result = gmail_watch(auth.gmail, email, topic)
        state["last_watch_at"] = time.time()
        if not state.get("history_id"):
            state["history_id"] = result.get("historyId")
        save_state(state)
    except HttpError as e:
        if e.resp.status == 401:
            auth.handle_401()
            result = gmail_watch(auth.gmail, email, topic)
            state["last_watch_at"] = time.time()
            if not state.get("history_id"):
                state["history_id"] = result.get("historyId")
            save_state(state)
        else:
            log.error("gmail watch failed: %s", e)


# --- main loop ----------------------------------------------------------------


def run(config_path: str) -> None:
    config = load_config(config_path)
    email = config["account"]
    subscription = config["pubsub"]["subscription"]
    project_id = config["pubsub"]["project_id"]

    log.info("gohook starting for %s", email)

    auth = AuthManager(config)
    state = load_state()

    _running = [True]

    def _stop(sig, frame):
        log.info("shutdown signal received")
        _running[0] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    maybe_renew_watch(auth, config, state)

    log.info("entering pull loop (subscription: %s)", subscription)

    while _running[0]:
        maybe_renew_watch(auth, config, state)

        try:
            messages = pubsub_pull(project_id, subscription, auth.creds, max_messages=10)
        except TokenExpiredError:
            auth.handle_401()
            continue
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.error("pubsub pull error: %s", e)
            time.sleep(5)
            continue

        if not messages:
            continue

        ack_ids = []
        for received in messages:
            ack_ids.append(received["ackId"])
            raw = received.get("message", {})
            data_b64 = raw.get("data", "")
            if not data_b64:
                continue
            import base64
            try:
                payload = json.loads(base64.b64decode(data_b64).decode())
            except Exception as e:
                log.warning("could not decode pubsub message: %s", e)
                continue
            log.debug("pubsub message: %s", payload)
            try:
                process_notification(auth, config, payload, state)
            except TokenExpiredError:
                auth.handle_401()
                try:
                    process_notification(auth, config, payload, state)
                except Exception as e2:
                    log.error("processing failed after token refresh: %s", e2)
            except Exception as e:
                log.error("error processing notification: %s", e)

        try:
            pubsub_ack(subscription, ack_ids, auth.creds)
        except Exception as e:
            log.warning("ack error (messages will redeliver): %s", e)

    log.info("gohook stopped")


# --- entrypoint ---------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Gmail Pub/Sub webhook daemon")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="enable debug logging"
    )
    parser.add_argument(
        "--auth", action="store_true",
        help="run OAuth2 authorization flow (opens browser), save token, then exit",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if not os.path.exists(args.config):
        log.error("config file not found: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)

    if args.auth:
        run_auth_flow(config)
        sys.exit(0)

    run(args.config)


if __name__ == "__main__":
    main()
