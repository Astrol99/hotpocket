# hotpocket

One Claude Code endpoint for the team, backed by a pool of the team's Claude
accounts. Developers point Claude Code at it once and stop dealing with usage
limits or accounts.

> **Before you use this:** Claude plans tie each seat to one person. Pooling
> seats or switching accounts to get past usage limits can break your plan's
> terms. Use this only if Anthropic has approved it for your organization.

- **Routing.** Each request goes to the account with the most headroom, read live
  from the API's usage headers. A session stays on its account while it has room.
- **Usage limits.** When an account hits its limit, the gateway resends the same
  request on the next account before the developer sees anything. If every account
  is out, Claude Code shows its normal "limit reached" message with the earliest reset.
- **Tokens.** The account tokens stay on the gateway. Each developer gets their
  own key, which can be revoked.

It runs with Docker Compose next to its own Tailscale node and is reachable only
over Tailscale, at `https://hotpocket.<your-tailnet>.ts.net`.

## For developers

You need to be on the team's Tailscale network. Open
`https://hotpocket.<your-tailnet>.ts.net`, click **Get my key**, and paste
the snippet it shows into `~/.claude/settings.json`. Then run `claude` as usual;
remove the snippet to go back to your own login.

- Use the full `.ts.net` name. The short `https://hotpocket` fails the HTTPS
  certificate check, and Claude Code just keeps retrying instead of showing an error.
- While you use the gateway, Claude Code turns off claude.ai connectors (Slack,
  Gmail, etc.), because the gateway's key takes priority over your claude.ai login.

## For the admin

On a machine with Docker (Compose 2.23 or later), turn on HTTPS certificates for
your tailnet (Tailscale admin console → DNS), then put an auth key (Settings → Keys)
in `.env`:

```sh
git clone https://github.com/Astrol99/hotpocket && cd hotpocket
echo TS_AUTHKEY=tskey-auth-... > .env
docker compose up -d --build        # also how you update: git pull, then this again
docker compose exec hotpocket hotpocket admin add you@example.com   # your Tailscale login
```

Without a key, open the newest link from `docker compose logs -f tailscale`; the
link changes every minute until it's used. Then, in the Tailscale admin console,
turn off key expiry for the `hotpocket` machine.

Everything else happens on the status page, `https://hotpocket.<your-tailnet>.ts.net`:

- **Accounts:** paste a token from `claude setup-token` for each Claude account.
  Saving under an existing label replaces its token, for example a rejected one.
- **Developer keys:** anyone on your tailnet can get their own. So adding someone
  to the tailnet (or sharing the machine with them) gives them access, and removing
  them takes it away. Admins can also make keys for others and revoke any key.
- Every change is logged with who made it: `docker compose logs -f hotpocket`.
  An update recreates the container and starts a new log, so save it first if you
  need the history.

`http://hotpocket` is a shortcut that redirects there. hotpocket learns who you
are from Tailscale serve, which reaches it on 127.0.0.1 in the Tailscale
container's network, so never publish its port or put another proxy in front of it.
`hotpocket admin --help` lists the admin commands.

## Development

```sh
uv run pytest                                  # tests, against a fake Anthropic API
HOTPOCKET_DATA=./data uv run hotpocket serve   # run it locally
```

Settings are environment variables: `HOTPOCKET_DATA` (where `hotpocket.db` lives),
`HOTPOCKET_HOST` (default `127.0.0.1`) and `HOTPOCKET_PORT` (default `8080`).
The database holds the accounts, developer keys (stored only as hashes), admins
and saved usage.

## License

MIT. See [LICENSE](LICENSE).
