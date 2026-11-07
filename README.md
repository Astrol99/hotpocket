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

The gateway runs in its own incus container (`hotpocket`) and is reachable
only over Tailscale at `https://hotpocket.<your-tailnet>.ts.net`.

**Requirements:** an incus host, Tailscale, and one long-lived token per account
from `claude setup-token`. The gateway is a small Python 3.10+ package built on
[aiohttp](https://docs.aiohttp.org/), managed with [uv](https://docs.astral.sh/uv/).
`deploy.sh` installs uv in the container and the exact versions in `uv.lock`.

## For developers

You need to be on the team's Tailscale network. Open
`https://hotpocket.<your-tailnet>.ts.net/_gateway`, click **Get my key**, and copy
the snippet it shows into `~/.claude/settings.json` (or ask an admin for a key):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://hotpocket.<your-tailnet>.ts.net",
    "ANTHROPIC_AUTH_TOKEN": "hp-..."
  }
}
```

Then run `claude` as usual. To go back to your own login, remove those two lines.
The key is shown once; if you lose it, click **Replace my key** for a new one (the
old one stops working).

Use the full `.ts.net` name. The short `https://hotpocket` fails the HTTPS
certificate check, and Claude Code just keeps retrying instead of showing an error.

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
incus exec hotpocket -- hotpocket account add dev     # paste the token (hidden)
```

To add several at once, pipe `label=token` lines into
`hotpocket account import`.

Developer keys: anyone signed in to your tailnet can get their own from the status
page (**Get my key**). The key is named after their Tailscale login, and they can
only replace their own. So adding someone to the tailnet is all it takes to give
them access, and so is sharing the hotpocket machine with someone from another
tailnet. You can also make and revoke keys yourself (name them after the person's
Tailscale login, so their own **Get my key** replaces it instead of adding a second):

```sh
incus exec hotpocket -- hotpocket key add alice@example.com   # prints the key once
incus exec hotpocket -- hotpocket key rm alice@example.com    # revoke
```

Status: open `https://hotpocket.<your-tailnet>.ts.net/_gateway` in a browser.
For the shorter `http://hotpocket/_gateway`, serve just the status page over plain
HTTP. The API stays HTTPS-only, so nobody can point Claude Code at plain HTTP by
mistake.

```sh
incus exec hotpocket -- tailscale serve --bg --http 80 --set-path /_gateway http://127.0.0.1:8080/_gateway
```

From the terminal:

```sh
incus exec hotpocket -- hotpocket status
incus exec hotpocket -- journalctl -u hotpocket -f    # request log
```

Changes to accounts and keys apply immediately, with no restart. Accounts are
tried in the order you added them when usage is tied.

### Managing from the browser

The status page can also add accounts, replace their tokens, remove them, and
create or revoke developer keys. First allow your Tailscale login (the page shows
it in the top corner):

```sh
incus exec hotpocket -- hotpocket admin add you@example.com
incus exec hotpocket -- hotpocket admin rm you@example.com     # take it away
```

Then open `https://hotpocket.<your-tailnet>.ts.net/_gateway`. Everyone else on
the tailnet can't change anything there except their own key. Some rules:

- Managing only works over HTTPS at the full address, never over the short
  `http://hotpocket` one.
- Tokens can be pasted in but are never shown again. A new developer key is
  shown once, with the `settings.json` snippet to send to the developer.
- Every change is logged with who made it (`journalctl -u hotpocket`).
- Your login comes from Tailscale serve on the same machine: hotpocket believes
  the login header on any connection from 127.0.0.1. So Tailscale serve must be
  the only thing that forwards connections to it there. Another reverse proxy, a
  port forward, or an incus proxy device pointed at 127.0.0.1:8080 would let
  anyone who reaches it claim an admin's login.

## Development

```sh
uv run tests/test_gateway.py                    # end-to-end tests
HOTPOCKET_DATA=./data uv run hotpocket serve   # run it locally
```

The tests run the gateway against a fake Anthropic API, so they need no real
tokens. Settings are environment variables: `HOTPOCKET_DATA` (default
`/var/lib/hotpocket`), `HOTPOCKET_HOST` (default `127.0.0.1`) and
`HOTPOCKET_PORT` (default `8080`).

## Notes

- Tokens from `claude setup-token` last a year. If one is revoked, the gateway
  skips it, logs `token rejected (401)`, and the status page shows it. Replace
  it with `account add`; the new token is used right away.
- In the Tailscale admin console, turn off key expiry for the `hotpocket`
  machine so it doesn't drop off the tailnet after 180 days.
- Files in the container: `/opt/hotpocket/` (code and its `.venv`, with
  `hotpocket` linked into `/usr/local/bin`) and `/var/lib/hotpocket/`
  (`accounts.json`, `keys.json`, `state.json`, all `0600`). The service runs as
  the unprivileged `hotpocket` user.
- `deploy.sh` creates the container if needed, copies the code in, and runs
  `setup.sh` inside it. Every step skips work that is already done, so a failed
  run can just be run again.
- `hardening.conf` re-enables `NoNewPrivileges`, `ProtectKernelTunables` and
  `ProtectControlGroups`, which the incus Ubuntu image turns off for every
  service by default.

## License

MIT. See [LICENSE](LICENSE).
