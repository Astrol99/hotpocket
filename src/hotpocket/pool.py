"""The pool: each account's usage and blocks, which account takes a request, and what each developer
used. It all runs on the event loop's one thread, so no locks are needed."""

import asyncio
import time
from collections import Counter

from aiohttp import web
from humanize import naturaldelta

from .log import log
from .store import key_hash, key_names, load_accounts, save_usage, saved_usage
from .usage import TOKEN_FIELDS, WINDOWS, api_cost, limits

STICKY_MAX_UTIL = 0.95  # a session moves off its account above this use
SESSION_TTL = 6 * 3600  # a session not seen for this long is forgotten
REASONS = {"auth": "token rejected", "limit": "usage limit reached"}  # an account's blocked_kind


def used(account, window, now):
    """An account's use of a window (0-1, None if unknown). Once its reset time passes it is back to 0."""
    reset = account.get(window + "_reset")
    return 0 if reset is not None and reset <= now else account.get(window)


class Pool:
    def __init__(self):
        saved = saved_usage()
        self.accounts = saved.get("accounts", {})  # label -> usage, block, and tallies
        self.devs = saved.get("devs", {})          # developer name -> tallies
        self.since = saved.get("since") or time.time()  # when the tallies started
        self.sessions = {}                          # Claude Code session id -> (label, last seen)
        self.wake = asyncio.Event()  # set (and replaced) on every change; open status pages wait on it
        self.dirty = False

    def touch(self, stopping=False):
        """Something the status page shows changed: save it at the next tick, and wake open pages now.
        When stopping, they are woken one last time, to close."""
        self.dirty = True
        if self.wake:
            self.wake.set()
            self.wake = None if stopping else asyncio.Event()

    def _acct(self, label):
        return self.accounts.setdefault(label, {"requests": 0})

    def _util(self, label):
        a, now = self.accounts.get(label, {}), time.time()
        return max(used(a, w, now) or 0 for w in WINDOWS)

    # ------------------------------------------------------------------ choosing an account

    def blocked(self, label, token, now):
        # A block belongs to the token that earned it, so replacing a token clears it. A rejected token stays
        # blocked until it works again (see choose), a usage limit until it resets.
        a = self.accounts.get(label, {})
        return (a.get("blocked_token") == key_hash(token)[:16]
                and (a.get("blocked_kind") == "auth" or a.get("blocked_until", 0) > now))

    def choose(self, accounts, session, exclude):
        """The account for a request's next try (None when there's none left): its session's account
        while that has room, else the one with the most."""
        now = time.time()
        left = [l for l in accounts if l not in exclude]
        ready = [l for l in left if not self.blocked(l, accounts[l], now)]
        if not ready:
            # A rejected token is only tried again when nothing else is left (the request would fail anyway),
            # so if Anthropic rejected every token at once, the pool is back as soon as it recovers. Only one
            # per request, the longest-rejected first: revoked tokens cost a request one call, not one each.
            rejected = [l for l in left if self.accounts[l].get("blocked_kind") == "auth"]
            if not rejected or any(self.accounts.get(l, {}).get("blocked_kind") == "auth" for l in exclude):
                return None
            return min(rejected, key=lambda l: self.accounts[l]["blocked_until"])
        sticky = self.sessions.get(session, (None,))[0]
        return (sticky if sticky in ready and self._util(sticky) < STICKY_MAX_UTIL
                else min(ready, key=self._util))  # ties go to the earlier-added account

    def first_back(self, labels):
        """When the first of these blocked accounts is back, and which limit it hit; None if every token
        was rejected (a rejection has no time it's back)."""
        timed = [a for l in labels if (a := self.accounts.get(l, {})).get("blocked_kind") != "auth"]
        if not timed:
            return None
        first = min(timed, key=lambda a: a.get("blocked_until", 0))
        return first.get("blocked_until", 0), first.get("blocked_claim")

    # ------------------------------------------------------------------ what the API said

    def observe(self, label, headers):
        """Record the account's live usage from a reply's headers."""
        a = self._acct(label)  # an account that answered is counted (0 requests) even before it serves one
        if heard := limits(headers):
            a.update(heard, usage_at=time.time())  # when it was last heard
            self.touch()

    def block(self, label, token, kind, until=None, claim=None):
        """Until a limit resets; a rejection has no end, and its time is when it happened."""
        self._acct(label).update(blocked_kind=kind, blocked_until=until or time.time(), blocked_claim=claim,
                                 blocked_token=key_hash(token)[:16])  # which token earned it
        self.touch()
        if until:
            log.info(f"account {label}: {REASONS[kind]}; back in {naturaldelta(until - time.time())}")
        else:
            log.warning(f"account {label}: {REASONS[kind]}; replace its token on the status page")

    def served(self, label, session, dev, model, usage):
        """An account served a request: it stays the session's account, and the request is counted."""
        a, d, now = self._acct(label), self.devs.setdefault(dev, {"requests": 0}), time.time()
        cost = api_cost(model, usage)
        self.sessions[session] = (label, now)  # where its prompt cache is
        # It works again, so a rejection is over; a usage limit another request hit meanwhile stays.
        if a.get("blocked_until", 0) <= now or a.get("blocked_kind") == "auth":
            a["blocked_kind"], a["blocked_until"] = None, 0
        windows = a.setdefault("spent", {})
        for w in WINDOWS:  # what each developer spent in the account's current windows, to split its use by
            spent = windows.get(w)
            if not spent or (spent["reset"] or 0) <= now:  # that window is over: this request starts the next
                spent = windows[w] = {"reset": a.get(w + "_reset"), "devs": {}}
            spent["devs"][dev] = spent["devs"].get(dev, 0) + cost
        for entry in (a, d):  # the same tally per account and per developer
            entry["requests"] += 1
            entry["cost"] = entry.get("cost", 0) + cost
            entry["tokens"] = Counter(entry.get("tokens"))
            entry["tokens"].update({f: usage.get(f) or 0 for f in TOKEN_FIELDS})
        d["last_seen"], d["last_account"] = now, label
        self.touch()

    # ------------------------------------------------------------------ the status page, and saving

    def snapshot(self, accounts, keys):
        """What the status page shows about the accounts and the developers (with these keys)."""
        now, rows, shares = time.time(), [], {w: Counter() for w in WINDOWS}
        for label, token in accounts.items():
            a = self.accounts.get(label, {})
            rows.append({
                "label": label, **{w: used(a, w, now) for w in WINDOWS},
                **{k: a.get(k) for k in ("usage_at", "requests", "tokens", "cost", *(w + "_reset" for w in WINDOWS))},
                **({"blocked_kind": a["blocked_kind"], "blocked_reason": REASONS[a["blocked_kind"]],
                    "blocked_until": a["blocked_until"]} if self.blocked(label, token, now) else {}),
            })
            # Developers' shares of the pool's limits: the account's use in its current window, split between
            # the developers who used it by what their requests would cost at API prices.
            for w in WINDOWS:
                spent, use = a.get("spent", {}).get(w), min(1, used(a, w, now) or 0)
                if spent and (spent["reset"] or 0) > now and (total := sum(spent["devs"].values())):
                    for dev, cost in spent["devs"].items():
                        shares[w][dev] += use * cost / total / len(accounts)
        devs = [{"name": n, **self.devs.get(n, {}), **{w: shares[w][n] for w in WINDOWS}} for n in keys]
        return {"accounts": rows, "developers": devs, "since": self.since}

    def tick(self):
        """Every few seconds: forget idle sessions and removed accounts and developers, and save if anything changed."""
        cutoff = time.time() - SESSION_TTL
        self.sessions = {s: v for s, v in self.sessions.items() if v[1] > cutoff}
        for kept, current in ((self.accounts, load_accounts()), (self.devs, key_names())):
            for gone in kept.keys() - set(current):
                del kept[gone]
                self.dirty = True
        if self.dirty:
            save_usage({"accounts": self.accounts, "devs": self.devs, "since": self.since})
            self.dirty = False


POOL = web.AppKey("pool", Pool)  # the app's one pool
