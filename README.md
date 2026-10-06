# starlings-agents

Reference adapters that put an agent runtime in a [Starlings](https://starlings.work)
workspace's Team. Each one implements the public contract every workspace
serves at `/help/howto/connect-an-agent-as-a-team-member`: the agent enrolls,
a workspace admin approves its code, and the agent collects its own keys.

| Runtime | Folder |
| --- | --- |
| [Hermes](https://hermes-agent.nousresearch.com/docs/) | [hermes](hermes) |

An adapter for another runtime is welcome. Keep it to the contract and to the
runtime's own API, with no secret in the repository.
