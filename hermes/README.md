# Hermes in Starlings

Puts a [Hermes](https://hermes-agent.nousresearch.com/docs/) agent you run in a
Starlings workspace's Team. People message it and hand it cards; it answers in
the conversation, as itself.

One file, Python 3.10+, no dependencies. The contract it implements is public:
`<workspace>/help/howto/connect-an-agent-as-a-team-member`.

```
Starlings ──signed hand-off──▶ your https endpoint ──▶ bridge.py serve :8790 ──▶ Hermes API :8642
Starlings ◀──signed reply──────────────────────────── bridge.py
```

A Hermes agent can also work in Starlings over MCP, as the person who pairs
it. That needs no bridge: see `<workspace>/help/howto/connect-an-agent-over-mcp`.

## 1. Turn on the Hermes API server

In `~/.hermes/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random value>
```

Restart the Hermes gateway. The API server listens on `127.0.0.1:8642`.

## 2. Give the bridge a public https address

Starlings refuses `http`. The bridge listens on `127.0.0.1:8790`; publish
only its three paths: `/meet/handoff`, `/manifest`, `/health`.

- **Cloudflare Tunnel:** `cloudflared tunnel create hermes`, route a hostname
  to it, and in `~/.cloudflared/config.yml`:

  ```yaml
  ingress:
    - hostname: hermes.example.com
      path: ^/(meet/handoff|manifest|health)$
      service: http://127.0.0.1:8790
    - service: http_status:404
  ```

- **Tailscale Funnel:** `tailscale funnel --bg 8790`. The bridge answers 404
  to every path but its three, and the Hermes API stays on loopback.

## 3. Join the workspace

```bash
API_SERVER_KEY=<same value> python3 hermes/bridge.py enroll https://<workspace> \
  --name Hermes --endpoint https://hermes.example.com/meet/handoff
```

It prints a code and a link. A workspace admin opens the link, checks that the
code matches, and approves. The bridge then writes the agent's address and
keys to `~/.config/starlings-hermes/env` (owner-only) and exits.

Add `API_SERVER_KEY=<same value>` to that file too.

## 4. Run it

```bash
python3 hermes/bridge.py serve
```

As a user service, so it survives reboots: copy `hermes/starlings-hermes.service`
to `~/.config/systemd/user/`, then `systemctl --user enable --now starlings-hermes`
(and `loginctl enable-linger $USER` once).

## What it does

- One Hermes session per Starlings conversation (`starlings-<thread id>`),
  resumed every turn, one turn at a time.
- Answers the hand-off at once (Starlings waits 5 seconds), then posts the
  reply, retrying with the same idempotency key.
- `cancel` and `reaction` start no turn.
- `/manifest` tells Starlings what the agent is. No secret is ever in it.

## Leaving

A workspace admin removes the agent in Admin › Agents. Its keys stop working at
once. To join again, enroll again.
