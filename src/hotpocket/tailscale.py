"""Who is asking, from the headers Tailscale serve sets. Serve is the only way in (it reaches hotpocket
on a unix socket), and it replaces these headers on every request, so they can be believed."""

import json
import logging

log = logging.getLogger(__name__)

ADMIN_CAP = "github.com/astrol99/hotpocket/admin"  # an app capability the tailnet policy grants admins


def viewer(headers):
    """Their Tailscale login (none for a tagged device), which may get its own key, and whether the tailnet
    policy makes them an admin, who manages the gateway."""
    login = (headers.get("tailscale-user-login") or "").lower() or None
    return {"login": login, "admin": ADMIN_CAP in json.loads(headers.get("tailscale-app-capabilities") or "{}")}


def source(request):
    """Who to name in a log line: the login, or for a tagged device its tailnet address."""
    return viewer(request.headers)["login"] or request.headers.get("x-forwarded-for")


def refused(request, why):
    """Log a request turned away: who sent it, what it was for, and why."""
    log.warning(f"refused {request.method} {request.rel_url.raw_path} from {source(request)}: {why}")
