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

VERSION = "0.3.0"
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
        self.mcp_port = int(config.get("MCP_PROXY_PORT", "8791"))
        # One turn at a time across every conversation: the MCP proxy names the
        # person whose turn is running, and Hermes does not say which session a
        # tool call belongs to. `actor` is that person, or None between turns.
        self.turn_lock = threading.Lock()
        self.actor: str | None = None
        self.actor_file = Path(config.get("_CONFIG_DIR", str(DEFAULT_CONFIG.parent))) / "last-actor"

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
        with self.turn_lock:
            self.actor = str(delivery.get("sender_email") or "").lower() or None
            if self.actor:
                try:
                    self.actor_file.write_text(self.actor)
                except OSError:
                    pass
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
            finally:
                self.actor = None

    def mcp(self, body: bytes, headers) -> tuple[int, bytes, dict[str, str]]:
        """Forward one MCP request to Starlings' agent door, signed as this agent for one person.

        During a turn the person is the one who asked. Between turns only listing
        is allowed (Hermes lists tools when it connects), named for the last
        person who asked; a call that reads or does something is refused.
        """
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            return 400, b'{"error":"invalid json"}', {"content-type": "application/json"}
        calls = parsed if isinstance(parsed, list) else [parsed]
        methods = {str(call.get("method", "")) for call in calls if isinstance(call, dict)}
        listing = {"initialize", "notifications/initialized", "ping", "tools/list", "resources/list",
                   "resources/templates/list", "prompts/list"}
        actor = self.actor
        if actor is None:
            if not methods <= listing:
                return self.mcp_refusal(calls, "Starlings tools work only inside a Starlings conversation, for the person who asked.")
            try:
                actor = self.actor_file.read_text().strip() or None
            except OSError:
                actor = None
            if actor is None:
                return self.mcp_refusal(calls, "Nobody has messaged this agent in Starlings yet.")
        timestamp = str(int(time.time() * 1000))
        forward = {
            "content-type": "application/json",
            "accept": headers.get("accept") or "application/json, text/event-stream",
            "user-agent": f"starlings-hermes/{VERSION}",
            "x-meet-agent": self.agent_email,
            "x-meet-actor": actor,
            "x-internal-key-id": self.agent_email,
            "x-internal-timestamp": timestamp,
            "x-internal-signature": hmac.new(
                self.inbound_key.encode(), f"{timestamp}.{self.agent_email}.{actor}.".encode() + body, hashlib.sha256,
            ).hexdigest(),
        }
        for name in ("mcp-session-id", "mcp-protocol-version"):
            if headers.get(name):
                forward[name] = headers[name]
        request = urllib.request.Request(f"{self.origin}/mcp", data=body, method="POST", headers=forward)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, response.read(), self.mcp_headers(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), self.mcp_headers(error.headers)
        except (urllib.error.URLError, TimeoutError) as error:
            log.error("starlings.mcp.failed error=%s", error)
            return 502, b'{"error":"Starlings did not answer"}', {"content-type": "application/json"}

    @staticmethod
    def mcp_headers(source) -> dict[str, str]:
        return {name: source[name] for name in ("content-type", "mcp-session-id") if source and source.get(name)}

    @staticmethod
    def mcp_refusal(calls: list, message: str) -> tuple[int, bytes, dict[str, str]]:
        errors = [{"jsonrpc": "2.0", "id": call.get("id"), "error": {"code": -32001, "message": message}}
                  for call in calls if isinstance(call, dict) and "id" in call]
        payload = errors if len(errors) != 1 else errors[0]
        return 200, json.dumps(payload).encode(), {"content-type": "application/json"}

    def hermes_get(self, path: str) -> dict | None:
        request = urllib.request.Request(
            f"{self.hermes_url}{path}",
            headers={"authorization": f"Bearer {self.hermes_key}", "user-agent": f"starlings-hermes/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                value = json.loads(response.read() or b"{}")
                return value if isinstance(value, dict) else None
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None

    def manifest(self) -> dict:
        """What Starlings shows on the agent's card in Team, read live from Hermes.

        The model, the enabled toolsets, the skills and the scheduled jobs. Each
        part is best effort: a Hermes endpoint that fails leaves that part out.
        Never a secret: no key, token, prompt or job text beyond its name.
        """
        out: dict = {
            "description": self.description,
            "home_url": "https://hermes-agent.nousresearch.com/docs/",
            "repo_url": "https://github.com/envisioning/starlings-agents",
            "version": VERSION,
        }
        model = self.hermes_get("/api/model/options") or {}
        if model.get("model"):
            out["model"] = f"{model['model']} ({model['provider']})" if model.get("provider") else str(model["model"])
        toolsets = (self.hermes_get("/v1/toolsets") or {}).get("data") or []
        tools = [
            {"name": str(row.get("name")), "description": str(row.get("description") or "")[:200]}
            for row in toolsets
            if isinstance(row, dict) and row.get("enabled") and row.get("name") and row.get("tools")
        ]
        if tools:
            out["tools"] = tools[:50]
        skills = (self.hermes_get("/v1/skills") or {}).get("data") or []
        named = [
            {"name": str(row.get("name")), **({"description": str(row["description"])[:200]} if row.get("description") else {})}
            for row in skills
            if isinstance(row, dict) and row.get("name")
        ]
        if named:
            out["skills"] = named[:50]
        jobs = (self.hermes_get("/api/jobs") or {}).get("jobs") or []
        schedules = [
            {"cron": str(job.get("schedule") or job.get("cron")), "description": str(job.get("name") or "")[:120]}
            for job in jobs
            if isinstance(job, dict) and (job.get("schedule") or job.get("cron"))
        ]
        if schedules:
            out["schedules"] = schedules[:20]
        return out


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
    config["_CONFIG_DIR"] = str(Path(args.config).parent)
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

    class McpHandler(BaseHTTPRequestHandler):
        """Loopback only: Hermes' `starlings` MCP server points here (see README)."""
        server_version = f"starlings-hermes/{VERSION}"

        def log_message(self, fmt, *log_args):
            log.info("mcp %s", fmt % log_args)

        def do_POST(self):
            if self.path.split("?")[0] != "/mcp":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                self.send_error(413)
                return
            status, body, headers = bridge.mcp(self.rfile.read(length), self.headers)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Streamable HTTP lets a server decline the standalone event stream.
            self.send_error(405)

        def do_DELETE(self):
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

    mcp_server = ThreadingHTTPServer(("127.0.0.1", bridge.mcp_port), McpHandler)
    threading.Thread(target=mcp_server.serve_forever, daemon=True).start()
    log.info("MCP proxy on 127.0.0.1:%s/mcp", bridge.mcp_port)

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
