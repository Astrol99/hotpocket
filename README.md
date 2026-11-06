# hotpocket

One Claude Code endpoint for the team, backed by a pool of the team's Claude
accounts. Developers point Claude Code at it once and stop dealing with usage
limits or accounts.

> **Before you use this:** Claude plans tie each seat to one person. Pooling
> seats or switching accounts to get past usage limits can break your plan's
> terms. Use this only if Anthropic has approved it for your organization.

- **Routing.** Each request goes to the account with the most headroom. A
  session stays on its account while that account has room.
- **Usage limits.** When an account hits its limit, the gateway resends the same
  request on the next account before the developer sees anything. There's no
  error, restart or "continue".
- **Tokens.** The account tokens stay on the gateway. Each developer gets their
  own key, which you can revoke.
- **Live usage.** The gateway reads each account's usage from the API's response
  headers, so it always knows how close every account is to its limit.
- **Everyone limited.** If every account is out, Claude Code shows its normal
  "limit reached" message with the time of the earliest reset.

The gateway runs in its own incus container (`claude-gateway`) and is reachable
only over Tailscale at `https://claude-gateway.<your-tailnet>.ts.net`.

**Requirements:** an incus host, Tailscale, and one long-lived token per account
from `claude setup-token`. The gateway is a single Python 3.8+ file with no
dependencies.

## For developers

Add this to `~/.claude/settings.json`, using the key your admin gives you:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://claude-gateway.<your-tailnet>.ts.net",
    "ANTHROPIC_AUTH_TOKEN": "cgw-..."
  }
}
```

Then run `claude` as usual. You need to be on Tailscale. To go back to your own
login, remove those two lines.

While you use the gateway, Claude Code turns off claude.ai connectors (Slack,
Gmail, etc.), because the gateway's key takes priority over your claude.ai login.

## For the admin

Clone this repo onto the incus host. The commands below run there. From another
machine, prefix them with `ssh <host>` (and run the deploy as
`HOST=<host> ./deploy.sh`).

Install or update:

```sh
cd hotpocket
./deploy.sh            # first run creates the container; later runs update the code
```

The first run ends with two one-time Tailscale commands (log in, then serve the
gateway over HTTPS). After that, turn off key expiry for the machine in the
Tailscale admin console.

Add accounts. For each account, run `claude setup-token` and open the link it
prints in a browser signed in to claude.ai as that account. Then save the token
it prints:

```sh
incus exec claude-gateway -- claude-gateway account add dev     # paste the token (hidden)
```

To add several at once, pipe `label=token` lines into
`claude-gateway account import`.

Developer keys:

```sh
incus exec claude-gateway -- claude-gateway key add alice    # prints the key once
incus exec claude-gateway -- claude-gateway key rm alice     # revoke
```

Status: open `https://claude-gateway.<your-tailnet>.ts.net/_gateway` in a browser, or:

```sh
incus exec claude-gateway -- claude-gateway status
incus exec claude-gateway -- journalctl -u claude-gateway -f    # request log
```

Changes to accounts and keys apply immediately, with no restart. Accounts are
tried in the order you added them when usage is tied.

## Tests

```sh
python3 tests/test_gateway.py
```

The tests run the gateway against a fake Anthropic API, so they need no real
tokens.

## Notes

- Tokens from `claude setup-token` last a year. If one is revoked, the gateway
  skips it, logs `token rejected (401)`, and the status page shows it. Replace
  it with `account add`.
- In the Tailscale admin console, turn off key expiry for the `claude-gateway`
  machine so it doesn't drop off the tailnet after 180 days.
- Files in the container: `/usr/local/bin/claude-gateway` (code) and
  `/var/lib/claude-gateway/` (`accounts.json`, `keys.json`, `state.json`, all
  `0600`). The service runs as the unprivileged `claude-gateway` user.
- `hardening.conf` re-enables `NoNewPrivileges`, which the incus Ubuntu image
  turns off for every service by default.
