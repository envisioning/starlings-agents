#!/usr/bin/env python3
"""Starlings bridge for Hermes: a Hermes agent as a Team member in any Starlings workspace.

Two commands:

  bridge.py enroll https://<workspace> --name Hermes --endpoint https://<public host>/meet/handoff
      Asks the workspace to let this agent join, prints a code for a workspace admin
      to approve, waits, and writes the agent's address and keys to the config file.

  bridge.py serve
      Takes Starlings' signed hand-offs, runs each turn in a Hermes session keyed on
      the Starlings conversation (one session per conversation, resumed every turn),
      and posts the answer back, signed with the agent's own key.

The contract is public: <workspace>/help/howto/connect-an-agent-as-a-team-member
Python 3.10+, standard library only.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.1"
SIGNATURE_SKEW_MS = 300_000
MAX_BODY_BYTES = 256_000
MESSAGE_MAX_CHARS = 4_000
POST_RETRY_DELAYS_S = [0.5, 1.5]
TURN_TIMEOUT_S = 20 * 60
DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "starlings-hermes" / "env"

log = logging.getLogger("starlings-hermes")


# ------------------------------------------------------------------ config


def read_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    # The environment wins, so a systemd EnvironmentFile or a container can override.
    for key in list(values) + ["API_SERVER_KEY", "HERMES_API_URL", "BRIDGE_HOST", "BRIDGE_PORT", "STARLINGS_AGENT_DESCRIPTION"]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def write_config(path: Path, updates: dict[str, str]) -> None:
    """Merge into the config file, created owner-only (0600): it holds the agent's keys."""
    values = read_config(path) if path.exists() else {}
    values.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write("# starlings-hermes: the agent's Starlings identity and keys. Never commit.\n")
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def require(config: dict[str, str], name: str) -> str:
    value = config.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Run `bridge.py enroll` first, or set it in the config file.")
    return value


# ------------------------------------------------------------------ http


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "user-agent": f"starlings-hermes/{VERSION}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw) if raw else {}
        except ValueError:
            return error.code, {}


# ------------------------------------------------------------------ enroll


def enroll(args: argparse.Namespace) -> None:
    workspace = args.workspace.rstrip("/")
    payload = {"name": args.name, "endpoint": args.endpoint, "runtime": f"hermes-bridge {VERSION}"}
    if args.description:
        payload["description"] = args.description
    status, answer = post_json(f"{workspace}/api/agents/enroll", payload)
    if status != 201:
        raise SystemExit(f"The workspace refused the request ({status}): {answer.get('error', answer)}")
    code = answer["user_code"]
    print(f"\n  Code: {code[:4]}-{code[4:]}\n  Ask a workspace admin to approve it at:\n  {answer['approve_url']}\n", flush=True)
    interval = max(int(answer.get("poll_interval_seconds", 5)), 2)
    while True:
        time.sleep(interval)
        status, polled = post_json(f"{workspace}/api/agents/enroll/poll", {"poll_token": answer["poll_token"]})
        if status == 429:
            interval += 2
            continue
        if status != 200:
            raise SystemExit(f"Polling failed ({status}): {polled.get('error', polled)}")
        state = polled.get("status")
        if state == "pending":
            continue
        if state != "approved":
            raise SystemExit(f"The request ended: {state}. Enroll again for a new code.")
        write_config(Path(args.config), {
            "STARLINGS_ORIGIN": polled["workspace_origin"],
            "STARLINGS_AGENT_EMAIL": polled["agent_email"],
            "AGENT_WEBHOOK_SECRET": polled["webhook_secret"],
            "INTERNAL_INBOUND_KEY": polled["inbound_key"],
        })
        print(f"  Approved. {polled['name']} is {polled['agent_email']} in {polled['workspace_origin']}.")
        print(f"  Keys written to {args.config} (owner-only). Start the bridge: bridge.py serve")
        return


# ------------------------------------------------------------------ serve


class Bridge:
    def __init__(self, config: dict[str, str]):
        self.webhook_secret = require(config, "AGENT_WEBHOOK_SECRET")
        self.inbound_key = require(config, "INTERNAL_INBOUND_KEY")
        self.origin = require(config, "STARLINGS_ORIGIN").rstrip("/")
        self.agent_email = require(config, "STARLINGS_AGENT_EMAIL").lower()
        self.hermes_url = config.get("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
        self.hermes_key = require(config, "API_SERVER_KEY")
        self.description = config.get(
            "STARLINGS_AGENT_DESCRIPTION", "A Hermes agent: terminal, files, browser, memory and its own skills.")
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()

    def verified(self, headers, body: bytes) -> bool:
        """Fails closed: no timestamp, a stale one, or a wrong signature is a refusal."""
        timestamp = headers.get("x-internal-timestamp", "")
        signature = headers.get("x-internal-signature", "").strip().lower()
        try:
            sent_at = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() * 1000 - sent_at) > SIGNATURE_SKEW_MS:
            return False
        return hmac.compare_digest(sign(self.webhook_secret, timestamp, body), signature)

    def say(self, thread_id: str, text: str, idempotency_key: str) -> bool:
        """Post into the conversation. The same key on every retry, so a retry says nothing twice."""
        flat = text.strip()
        clipped = flat if len(flat) <= MESSAGE_MAX_CHARS else flat[: MESSAGE_MAX_CHARS - 1] + "…"
        body = json.dumps({"thread_id": thread_id, "type": "text", "sender": self.agent_email,
                           "text": clipped, "idempotency_key": idempotency_key}).encode()
        for attempt, delay in enumerate([*POST_RETRY_DELAYS_S, None]):
            timestamp = str(int(time.time() * 1000))
            request = urllib.request.Request(
                f"{self.origin}/api/internal/channel-events", data=body, method="POST",
                headers={
                    "content-type": "application/json",
                    # Named, never urllib's default: Cloudflare's browser check refuses
                    # `Python-urllib` with a 403 (error 1010) before Starlings sees it.
                    "user-agent": f"starlings-hermes/{VERSION}",
                    "x-internal-timestamp": timestamp,
                    "x-internal-signature": sign(self.inbound_key, timestamp, body),
                    "x-internal-key-id": self.agent_email,
                },
            )
            retryable = False
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    if 200 <= response.status < 300:
                        return True
            except urllib.error.HTTPError as error:
                # Status only: the text belongs to a person, not to this log.
                log.error("starlings.post.rejected status=%s attempt=%s", error.code, attempt + 1)
                retryable = error.code == 429 or error.code >= 500
            except (urllib.error.URLError, TimeoutError) as error:
                log.error("starlings.post.failed attempt=%s error=%s", attempt + 1, error)
                retryable = True
            if not retryable or delay is None:
                return False
            time.sleep(delay)
        return False

    def hermes(self, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
        return post_json(f"{self.hermes_url}{path}", payload, {"authorization": f"Bearer {self.hermes_key}"}, timeout)

    def turn(self, delivery: dict) -> None:
        prompt = prompt_for(delivery)
        if prompt is None:
            return
        thread_id = delivery["thread_id"]
        key = f"hermes:{delivery.get('message_id') or thread_id}"
        with self.locks_guard:
            lock = self.locks.setdefault(thread_id, threading.Lock())
        # One turn at a time per conversation, so two messages answer in order.
        with lock:
            try:
                session_id = "starlings-" + re.sub(r"[^A-Za-z0-9_-]", "-", thread_id)[:100]
                status, _ = self.hermes("/api/sessions", {
                    "id": session_id, "title": f"Starlings {delivery.get('thread_kind', 'thread')} {thread_id}", "source": "starlings",
                }, 30)
                if status not in (200, 201, 409):  # 409: the session exists, the conversation resumes
                    raise RuntimeError(f"Hermes answered {status} to the session")
                status, answer = self.hermes(f"/api/sessions/{session_id}/chat", {
                    "message": prompt,
                    "author": {"name": delivery.get("sender_name") or "", "id": delivery.get("sender_email") or ""},
                }, TURN_TIMEOUT_S)
                if status != 200:
                    raise RuntimeError(f"Hermes answered {status} to the turn")
                reply = str(((answer.get("message") or {}).get("content")) or "").strip()
                self.say(thread_id, reply or "I have nothing to add.", key)
            except Exception as error:  # noqa: BLE001 — the person must hear that the turn failed
                log.exception("turn.failed thread=%s", thread_id)
                self.say(thread_id, f"I could not finish this turn: {error}", key)

    def manifest(self) -> dict:
        """What Starlings shows on the agent's card in Team. Never a secret."""
        return {
            "description": self.description,
            "home_url": "https://hermes-agent.nousresearch.com/docs/",
            "version": VERSION,
        }


def prompt_for(delivery: dict) -> str | None:
    """The turn's text, or None for a hand-off that starts no turn."""
    kind = delivery.get("kind") or "message"
    sender = f"{delivery.get('sender_name') or delivery.get('sender_email')} <{delivery.get('sender_email')}>"
    card = "".join(f"\n[Card: {ref.get('kind')} {ref.get('id')}]" for ref in delivery.get("refs") or [])
    if kind == "message" and delivery.get("text"):
        return f"[Starlings, from {sender}]{card}\n{delivery['text']}"
    if kind == "question_answer":
        labels = ", ".join(delivery.get("option_labels") or [])
        return f"[Starlings, from {sender}] answered your question \"{delivery.get('question_text', '')}\": {labels}"
    # `cancel`, `reaction` and any kind this version does not know: acknowledged, no turn.
    return None


def serve(args: argparse.Namespace) -> None:
    config = read_config(Path(args.config))
    bridge = Bridge(config)
    host = config.get("BRIDGE_HOST", "127.0.0.1")
    port = int(config.get("BRIDGE_PORT", "8790"))

    class Handler(BaseHTTPRequestHandler):
        server_version = f"starlings-hermes/{VERSION}"

        def log_message(self, fmt, *log_args):  # request lines only, never bodies
            log.info("%s %s", self.address_string(), fmt % log_args)

        def reply(self, status: int, payload: dict | None = None) -> None:
            body = json.dumps(payload or {}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"ok": True})
            if self.path == "/manifest":
                return self.reply(200, bridge.manifest())
            return self.reply(404)

        def do_POST(self):
            if self.path != "/meet/handoff":
                return self.reply(404)
            length = int(self.headers.get("content-length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                return self.reply(413 if length > MAX_BODY_BYTES else 400)
            body = self.rfile.read(length)
            if not bridge.verified(self.headers, body):
                return self.reply(401)
            try:
                delivery = json.loads(body)
            except ValueError:
                return self.reply(400)
            if not isinstance(delivery, dict) or not delivery.get("thread_id"):
                return self.reply(400)
            if str(delivery.get("agent_email", "")).lower() != bridge.agent_email:
                return self.reply(403)
            # Acknowledge inside Starlings' five seconds; the answer follows on its own route.
            threading.Thread(target=bridge.turn, args=(delivery,), daemon=True).start()
            return self.reply(202, {"accepted": True})

    server = ThreadingHTTPServer((host, port), Handler)
    log.info("listening on %s:%s as %s in %s", host, port, bridge.agent_email, bridge.origin)
    server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="bridge.py", description="Starlings bridge for Hermes.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help=f"config file (default {DEFAULT_CONFIG})")
    commands = parser.add_subparsers(dest="command", required=True)
    joining = commands.add_parser("enroll", help="ask a workspace to let this agent join its Team")
    joining.add_argument("workspace", help="the workspace address, e.g. https://hq.starlings.work")
    joining.add_argument("--name", required=True, help="what the agent asks to be called")
    joining.add_argument("--endpoint", required=True, help="the public https URL of /meet/handoff on this bridge")
    joining.add_argument("--description", help="one or two sentences for the admin")
    commands.add_parser("serve", help="take hand-offs and answer them")
    args = parser.parse_args()
    if args.command == "enroll":
        enroll(args)
    else:
        serve(args)


if __name__ == "__main__":
    sys.exit(main())
