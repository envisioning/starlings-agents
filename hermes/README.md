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

## 1. Give the Team member its own Hermes profile

Everyone in the workspace can message this agent, so it runs in a profile of
its own, not in the one you use yourself:

```bash
hermes profile create starlings --clone-from default --no-alias
```

Then, in `~/.hermes/profiles/starlings/`:

- **`.env`:** add `API_SERVER_ENABLED=true` and `API_SERVER_KEY=<a long random value>`.
  Remove every key the team should not reach through it (a database
  service role, a payment key). The model provider's key stays.
- **`config.yaml`:** remove what reaches your own machine: `terminal.docker_volumes`,
  device arguments in `terminal.docker_extra_args`, SSH keys, host hints.
  Turn off toolsets the team should not drive, for example
  `agent.disabled_toolsets: [homeassistant]`.
- **`config.yaml`:** add Starlings as an MCP server, through the bridge:

  ```yaml
  mcp_servers:
    starlings:
      url: http://127.0.0.1:8791/mcp
      timeout: 120
  ```

A multiplexed gateway serves the profile at `http://127.0.0.1:8642/p/starlings`
within 30 seconds; a gateway per profile serves it on the profile's own
`API_SERVER_PORT`.

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

Add the profile's `API_SERVER_KEY` to that file too, and
`HERMES_API_URL=http://127.0.0.1:8642/p/starlings` (or the profile's own port).

## 4. Run it

```bash
python3 hermes/bridge.py serve
```

As a user service, so it survives reboots: copy `hermes/starlings-hermes.service`
to `~/.config/systemd/user/`, then `systemctl --user enable --now starlings-hermes`
(and `loginctl enable-linger $USER` once).

## Starlings tools, for the person who asked

The bridge runs a local MCP server on `127.0.0.1:8791/mcp` that forwards to the
workspace's `/mcp` agent door, signed with the agent's own key and naming the
person whose message Hermes is answering. Hermes then reads Starlings as that
person: their projects, calls, tasks and documents, never more than they can
open, and read only.

- A tool call outside a Starlings turn is refused. Listing tools when Hermes
  connects is allowed, named for the last person who messaged the agent.
- Turns run one at a time across all conversations, because Hermes does not
  tell an MCP server which session a call belongs to.

Your own Hermes, as you, is the other way in: see
`<workspace>/help/howto/connect-an-agent-over-mcp`.

## What it does

- One Hermes session per Starlings conversation (`starlings-<thread id>`),
  resumed every turn.
- Answers the hand-off at once (Starlings waits 5 seconds), then posts the
  reply, retrying with the same idempotency key.
- Sends `User-Agent: starlings-hermes/<version>` on every request. Cloudflare
  refuses Python's default user agent with a 403 (error 1010).
- `cancel` and `reaction` start no turn.
- `/manifest` tells Starlings what the agent is, read live from Hermes: the
  model, the enabled toolsets, the skills and the scheduled jobs. No secret is
  ever in it.

## Leaving

A workspace admin removes the agent in Admin › Agents. Its keys stop working at
once. To join again, enroll again.
