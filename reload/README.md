# claude-reload

Runs Claude Code across several of the team's Claude accounts. When the account
you're on hits its usage limit, it moves the same conversation to the account
with the most room left and keeps going.

Works on macOS and Linux. Needs Python 3.8+ and Claude Code.

## Install

```sh
git clone https://github.com/Astrol99/hotpocket.git ~/.hotpocket
~/.hotpocket/reload/install.sh
```

This links `claude-reload` into `~/.local/bin`. Get updates later with
`claude-reload update`.

## Add accounts

**From your admin (fastest).** Your admin sends a file of `label=token` lines
through the password manager. Import it, then delete the file:

```sh
claude-reload import tokens.txt && rm tokens.txt
```

**Yourself.** Run `claude-reload add <label>`. Claude Code opens a browser (or
prints a link on a remote machine) to authorize a token. Sign in to claude.ai as
the account you're adding first. A private window helps, because otherwise you
get a token for whatever account is already signed in.

Check what you have:

```sh
claude-reload status
```

```
ACCOUNT  5H   7D   5H RESETS  7D RESETS  STATE
dev      61%  23%  3:50pm     Sat 2:00am ready
admin    12%  40%  5:10pm     Sun 9:00am ready
```

## Use

Run `claude-reload` anywhere you'd run `claude`. All `claude` flags and prompts
pass through:

```sh
claude-reload
claude-reload --model opus "fix the flaky login test"
claude-reload -c                  # continue the last conversation
claude-reload --use admin         # start on a specific account
```

Optionally, make it your default `claude` (the wrapper still finds the real
one on your PATH):

```sh
alias claude=claude-reload                  # bash / zsh
alias --save claude claude-reload           # fish
```

## What happens at a usage limit

1. A Claude Code hook (`StopFailure`, matcher `rate_limit`) notices the turn
   failed on a usage limit and stops that session.
2. The account is marked as limited until its reset time. The time comes from
   the usage API, then from the error message, and falls back to 5 hours.
3. The same session is resumed (`--resume`) on the account with the most
   headroom, with a prompt to continue without redoing finished work. Your
   other flags (`--model`, permission mode, etc.) are kept.
4. If every account is limited, it waits for the first reset and then
   continues. Press Ctrl-C to stop waiting; it prints the command to resume later.

## Commands

| Command | What it does |
| --- | --- |
| `claude-reload [--use LABEL] [claude args]` | Run Claude Code with account rotation |
| `claude-reload add LABEL [--token-stdin]` | Add or replace an account |
| `claude-reload import [FILE]` | Add accounts from `label=token` lines (stdin if no file) |
| `claude-reload export [LABEL...]` | Print accounts as `label=token` lines (for the admin to share) |
| `claude-reload status` | Accounts, usage, reset times |
| `claude-reload rm LABEL` | Remove an account |
| `claude-reload reset [LABEL]` | Clear "limited" marks |
| `claude-reload update` | `git pull` the latest version |

## Where things live

- **Tokens:** the macOS Keychain (service `claude-reload`) on macOS. On Linux,
  `~/.claude-reload/tokens.json` with `0600` permissions, which is how Claude
  Code stores its own Linux login. Set `CLAUDE_RELOAD_STORE=file` or
  `keychain` to override.
- **State:** `~/.claude-reload/` holds the account order (`config.json`),
  limited-until times (`state.json`), and a log of limit events (`events.log`).
- Your normal `claude` login isn't touched. The tokens go only to the
  `claude` processes that `claude-reload` starts.

## Notes

- Tokens from `claude setup-token` last a year. If one is revoked or expires,
  run `claude-reload add <label>` again.
- If `ANTHROPIC_API_KEY` is set, it's ignored inside `claude-reload` sessions.
  Otherwise it would take priority over the account tokens.
- Keep Claude Code's "Continue automatically at usage limit" setting off. With
  it on, Claude Code may wait at the limit instead of failing, and then no
  switch happens.
- Passing your own `--settings` flag replaces the one `claude-reload` adds,
  which turns the hook off.
