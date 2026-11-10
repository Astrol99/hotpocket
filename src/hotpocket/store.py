"""The database, hotpocket.db in HOTPOCKET_DATA: the pool's accounts and their tokens, the developers'
keys, and the pool's saved usage. Edits from the status page are checked here."""

import functools
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from .usage import PLANS

DATA = Path(os.environ.get("HOTPOCKET_DATA", "/data"))

SCHEMA = """
-- Accounts are tried in `position` order when their usage is tied.
CREATE TABLE IF NOT EXISTS accounts (label TEXT PRIMARY KEY, token TEXT NOT NULL, position INTEGER NOT NULL);
-- Developer keys; requests are matched by the hash.
CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
-- Each key itself, so its owner can set up more devices with it. Apart from `keys` so an older hotpocket still
-- runs on this database; a key that one makes has no row here, or one that no longer matches its hash.
CREATE TABLE IF NOT EXISTS kept_keys (name TEXT PRIMARY KEY, key TEXT NOT NULL);
-- Each seat's plan (a key of usage.PLANS), apart from `accounts` for the same reason.
CREATE TABLE IF NOT EXISTS plans (label TEXT PRIMARY KEY, plan TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);    -- the pool's saved usage (JSON)
"""

# Account labels and developer names. Developer names are often Tailscale logins
# (you@example.com, you@github); all-dot names are refused because "." and ".." break URLs.
NAME_RE = re.compile(r"(?!\.+$)[\w.%+@-]{1,254}", re.ASCII)
TOKEN_RE = re.compile(r"sk-ant-[a-z]+[0-9]*-[A-Za-z0-9_-]+")  # a seat's token (sk-ant-oat...), or a Console API key


class EditError(Exception):
    """A problem with an edit, to show on the status page."""


@functools.cache
def db():
    """The database connection (creating the database on first use)."""
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never waits long: a stuck writer (say an open sqlite3 shell) would stall every stream. A failed
    # usage save retries at the next tick.
    con = sqlite3.connect(DATA / "hotpocket.db", timeout=1)
    con.execute("PRAGMA journal_mode = WAL")  # someone reading it in a sqlite3 shell doesn't hold up writes
    try:
        con.executescript(SCHEMA)  # a new table needs the write lock: may fail while locked
    except sqlite3.Error:
        con.close()  # not cached, so the next call opens another
        raise
    return con


def write(sql, *params):
    """Run one change; returns how many rows it touched."""
    with db() as con:
        return con.execute(sql, params).rowcount


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def load_accounts():
    """{label: token}, in the order the accounts were added."""
    return dict(db().execute("SELECT label, token FROM accounts ORDER BY position"))


def load_plans():
    """{label: plan} for the accounts whose plan was set."""
    return dict(db().execute("SELECT label, plan FROM plans"))


def key_names():
    return [name for (name,) in db().execute("SELECT name FROM keys ORDER BY created")]


def key_owner(key):
    """The developer a key belongs to, or None."""
    row = db().execute("SELECT name FROM keys WHERE hash = ?", (key_hash(key),)).fetchone()
    return row and row[0]


def saved_usage():
    row = db().execute("SELECT value FROM state WHERE name = 'pool'").fetchone()
    return json.loads(row[0]) if row else {}


def save_usage(usage):
    write("INSERT OR REPLACE INTO state VALUES ('pool', ?)", json.dumps(usage))


def check_name(name, what):
    if not NAME_RE.fullmatch(name):
        raise EditError(f"{what} names are letters, digits and . _ - @ + % (not only dots, at most 254).")


def save_account(label, raw_token, plan=None):
    """Add an account, or change one: its token (blank keeps the one it has) and its plan ("" clears it, None leaves
    it as it is). A changed account keeps its place."""
    check_name(label, "Account")
    token = "".join(raw_token.split())
    if token and not TOKEN_RE.fullmatch(token):
        raise EditError("That doesn't look like a Claude Code token or an API key (expected sk-ant-...).")
    if plan and plan not in PLANS:
        raise EditError(f"Plans are {', '.join(PLANS)}.")
    new = label not in load_accounts()
    if new and not token:
        raise EditError("A new account needs a token.")
    with db() as con:
        if new:  # not a plan left behind by an account an older hotpocket removed
            con.execute("DELETE FROM plans WHERE label = ?", (label,))
        if token:
            con.execute("INSERT INTO accounts VALUES (?, ?, (SELECT IFNULL(MAX(position), 0) + 1 FROM accounts)) "
                        "ON CONFLICT (label) DO UPDATE SET token = excluded.token", (label, token))
        if plan:
            con.execute("INSERT OR REPLACE INTO plans VALUES (?, ?)", (label, plan))
        elif plan == "":
            con.execute("DELETE FROM plans WHERE label = ?", (label,))


def is_api_key(token):
    """A Claude Console API key, paid from credits, rather than a seat's subscription token (sk-ant-oat...).
    Console keys come in several kinds: sk-ant-api..., a personal key's sk-ant-usr..., and so on."""
    return not token.startswith("sk-ant-oat")


def remove_account(label):
    with db() as con:
        con.execute("DELETE FROM plans WHERE label = ?", (label,))
        if not con.execute("DELETE FROM accounts WHERE label = ?", (label,)).rowcount:
            raise EditError(f"No account named '{label}'.")


def new_key(name):
    """A new key for a developer (replacing any they had)."""
    check_name(name, "Developer")
    key = "hp-" + secrets.token_urlsafe(24)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO keys VALUES (?, ?, ?)", (name, key_hash(key), time.time()))
        con.execute("INSERT OR REPLACE INTO kept_keys VALUES (?, ?)", (name, key))
    return key


def stored_key(name):
    """A developer's key, to set up another device; None if it wasn't kept (made before keys were, or by an
    older hotpocket), so the page never shows a key that doesn't work."""
    row = db().execute("SELECT hash, key FROM keys LEFT JOIN kept_keys USING (name) WHERE name = ?",
                       (name,)).fetchone()
    if not row:
        raise EditError(f"No developer key named '{name}'.")
    hash_, key = row
    return key if key and key_hash(key) == hash_ else None


def remove_key(name):
    with db() as con:  # their history goes at the pool's next tick
        con.execute("DELETE FROM kept_keys WHERE name = ?", (name,))
        if not con.execute("DELETE FROM keys WHERE name = ?", (name,)).rowcount:
            raise EditError(f"No developer key named '{name}'.")
