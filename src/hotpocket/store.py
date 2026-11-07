"""The database, hotpocket.db in HOTPOCKET_DATA: the pool's accounts and their tokens, the developers'
keys (only as hashes), and the pool's saved usage. Edits from the status page are checked here."""

import functools
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

DATA = Path(os.environ.get("HOTPOCKET_DATA", "/data"))

SCHEMA = """
-- Accounts are tried in `position` order when their usage is tied.
CREATE TABLE IF NOT EXISTS accounts (label TEXT PRIMARY KEY, token TEXT NOT NULL, position INTEGER NOT NULL);
-- Only a hash of each developer key is kept; the key itself is shown once and never stored.
CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);    -- the pool's saved usage (JSON)
"""

# Account labels and developer names. Developer names are often Tailscale logins
# (you@example.com, you@github); all-dot names are refused because "." and ".." break URLs.
NAME_RE = re.compile(r"(?!\.+$)[\w.%+@-]{1,254}", re.ASCII)
TOKEN_RE = re.compile(r"sk-ant-(?:oat|api)[0-9]*-[A-Za-z0-9_-]+")  # a seat's token, or a Console API key


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
    con.executescript(SCHEMA)
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


def save_account(label, raw_token):
    """Add an account or replace its token (a replaced account keeps its place)."""
    check_name(label, "Account")
    token = "".join(raw_token.split())
    if not TOKEN_RE.fullmatch(token):
        raise EditError("That's neither a Claude Code token (sk-ant-oat...) nor an API key (sk-ant-api...).")
    write("INSERT INTO accounts VALUES (?, ?, (SELECT IFNULL(MAX(position), 0) + 1 FROM accounts)) "
          "ON CONFLICT (label) DO UPDATE SET token = excluded.token", label, token)


def is_api_key(token):
    """A Claude Console API key, paid from credits, rather than a seat's subscription token."""
    return token.startswith("sk-ant-api")


def remove_account(label):
    if not write("DELETE FROM accounts WHERE label = ?", label):
        raise EditError(f"No account named '{label}'.")


def new_key(name):
    """A new key for a developer (replacing any they had); only its hash is kept."""
    check_name(name, "Developer")
    key = "hp-" + secrets.token_urlsafe(24)
    write("INSERT OR REPLACE INTO keys VALUES (?, ?, ?)", name, key_hash(key), time.time())
    return key


def remove_key(name):
    if not write("DELETE FROM keys WHERE name = ?", name):  # their history goes at the pool's next tick
        raise EditError(f"No developer key named '{name}'.")
