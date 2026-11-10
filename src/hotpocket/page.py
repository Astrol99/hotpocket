"""The status page (static/page.*): its files, the data it shows, kept current over a live connection,
and the edits made on it: accounts and anyone's key by admins, and anyone's own key."""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from aiohttp import web

from .pool import POOL
from .proxy import api_error
from .store import (EditError, key_names, load_accounts, load_plans, new_key, remove_account, remove_key,
                    save_account, stored_key)
from .usage import PLANS
from .tailscale import refused, source, viewer

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer",
    # No framing (clickjacking), only the page's own script and styles, and forms only submit through it.
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}

routes = web.RouteTableDef()


@routes.get("/")
async def status_page(_request):
    return web.FileResponse(STATIC / "page.html", headers=PAGE_HEADERS)


@routes.get(r"/_gateway/{name:page\.css|page\.js}")
async def page_file(request):
    # Checked again on every load (cheaply, by its ETag), so the page never runs an old copy after an update.
    return web.FileResponse(STATIC / request.match_info["name"], headers={"cache-control": "no-cache"})


PLAN_NAMES = {plan: name for plan, (name, _) in PLANS.items()}


def page_data(request):
    return {**request.app[POOL].snapshot(load_accounts(), key_names(), load_plans()), "viewer": viewer(request.headers),
            "plans": PLAN_NAMES}


@routes.get("/_gateway/status")
async def status_json(request):
    return web.json_response(page_data(request), dumps=lambda d: json.dumps(d, indent=2))


@routes.get("/_gateway/events", allow_head=False)  # a HEAD reply ends at the headers, while this would write on
async def events(request):
    """Server-sent events: the page's data as soon as it opens, again when it changes (at most once a
    second), and every 30 s anyway, for blocks and limits that run out."""
    pool = request.app[POOL]
    resp = web.StreamResponse(headers={"content-type": "text/event-stream", "cache-control": "no-store"})
    await resp.prepare(request)
    try:
        while wake := pool.wake:  # the next change, taken before sending so none is missed; None at shutdown
            await resp.write(f"data: {json.dumps(page_data(request))}\n\n".encode())  # fails once the page is gone
            await asyncio.sleep(1)
            try:
                await asyncio.wait_for(wake.wait(), 29)
            except TimeoutError:
                pass
    except (ConnectionError, sqlite3.OperationalError):  # the page is gone, or the database busy: it reconnects
        pass
    return resp


async def close_events(app):  # open pages would otherwise hold up a restart for its full timeout
    app[POOL].touch(stopping=True)


# --------------------------------------------------------------------------- edits

ROLES = {"admin": "Only hotpocket admins can do this.",  # why a viewer without the role is refused
         "login": "Tailscale didn't say who you are (a tagged device?), so open this page on your own device."}


def page_api(role, changes=True):
    """An endpoint the page calls, for a viewer with `role` (a login, or admin). The handler gets the URL's
    {name} and returns what it did, for the log, and the JSON to answer with. A read (changes=False) is logged
    but not pushed to open pages."""
    def wrap(handler):
        async def wrapped(request):
            origin = request.headers.get("origin")
            why = None
            if not viewer(request.headers)[role]:
                why = ROLES[role]
            # Stops other sites making a signed-in browser call this. (Serve passes on the host the browser asked for.)
            elif origin != f"https://{request.headers.get('x-forwarded-host')}":
                why = f"request came from {origin or 'no page'}, not the status page"
            if why:
                refused(request, why)
                return api_error(403, "permission_error", ROLES[role])
            try:
                done, result = await handler(request, **request.match_info)
            except EditError as e:
                return api_error(400, "invalid_request_error", str(e))
            except sqlite3.OperationalError as e:  # e.g. locked by a long write elsewhere
                refused(request, f"database error ({e})")
                return api_error(503, "overloaded_error", "The database is busy; try again in a moment.")
            log.info(f"{'admin ' if role == 'admin' else ''}{source(request)}: {done}")
            if changes:
                request.app[POOL].touch()  # open pages show the change right away
            return web.json_response(result, headers={"cache-control": "no-store"})  # may hold a key
        return wrapped
    return wrap


@routes.put("/_gateway/api/accounts/{name}")
@page_api("admin")
async def put_account(request, name):
    try:
        body = await request.json()
        token, plan = str(body.get("token") or ""), body.get("plan")
    except (ValueError, AttributeError):
        raise EditError('Expected a JSON body with "token" and "plan".') from None
    if plan is not None:  # left out: the plan stays as it is
        plan = str(plan)
    save_account(name, token, plan)
    return f"saved account {name}" + (f" ({plan})" if plan else ""), {}


@routes.delete("/_gateway/api/accounts/{name}")
@page_api("admin")
async def delete_account(_request, name):
    remove_account(name)
    return f"removed account {name}", {}


@routes.post("/_gateway/api/keys/{name}")
@page_api("admin")
async def post_key(_request, name):
    name = name.lower()  # developer names are usually Tailscale logins, which ignore case
    return f"created a key for {name}", {"key": new_key(name)}


@routes.delete("/_gateway/api/keys/{name}")
@page_api("admin")
async def delete_key(_request, name):
    name = name.lower()
    remove_key(name)
    return f"removed developer {name}", {}


@routes.post("/_gateway/api/my-key")
@page_api("login")
async def post_my_key(request):
    # Anyone on the tailnet can get (or replace) the key named after their own login, and only that one.
    return "created their own key", {"key": new_key(viewer(request.headers)["login"])}


# A POST, like every call here, so the browser sends the Origin that page_api checks.
@routes.post("/_gateway/api/my-key/show")
@page_api("login", changes=False)
async def show_my_key(request):
    # The same key on every device: anyone can see their own again (only their own), to set up another one.
    # {} when they have none, so the page makes one without asking even if it hadn't heard of a key.
    try:
        return "showed their own key", {"key": stored_key(viewer(request.headers)["login"])}
    except EditError:
        return "asked to see their key, but has none", {}
