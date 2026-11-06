# hotpocket

Tools for running Claude Code across a pool of Claude accounts, so developers
keep working when one account hits its usage limit.

> **Before you use this:** Claude plans tie each seat to one person. Pooling
> seats or switching accounts to get past usage limits can break your plan's
> terms. Use these tools only if Anthropic has approved this for your
> organization.

There are two tools. Pick one.

## [`gateway/`](gateway/): team gateway (recommended for teams)

A small proxy you host once, for example in an incus container reachable over
Tailscale. Developers point Claude Code at it with two settings and install
nothing else.

- Each request goes to the account with the most headroom.
- When an account hits its limit, the gateway resends the same request on
  another account, so the developer never sees the error.
- Account tokens stay on the server. Each developer gets their own revocable key.
- A status page shows each account's live 5-hour and 7-day usage.

See [gateway/README.md](gateway/README.md).

## [`reload/`](reload/): `claude-reload` (single machine)

A wrapper you run in place of `claude` on your own machine. When the current
account hits its limit, it stops the session and resumes the same conversation
on the next account. Tokens are stored in the macOS Keychain, or in a `0600`
file on Linux.

See [reload/README.md](reload/README.md).

## Requirements

- Python 3.8+ (standard library only, no dependencies)
- Claude Code
- One long-lived token per account, from `claude setup-token`
- For the gateway: an incus host and Tailscale

## Tests

```sh
python3 gateway/tests/test_gateway.py
```

The tests run the gateway against a fake Anthropic API, so they need no real
tokens.
