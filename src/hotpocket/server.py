"""Starts hotpocket: the routes, the pool and the HTTP client for the API, and the unix socket that
Tailscale serve reaches it through."""

import asyncio
import os
import socket
import sqlite3

from aiohttp import web

from . import page, proxy
from .log import log
from .pool import POOL, Pool
from .store import key_names, load_accounts

# Tailscale serve's way in, and the only one: whoever reaches it is believed about who they are.
SOCKET = os.environ.get("HOTPOCKET_SOCKET", "/run/hotpocket/hotpocket.sock")
MAX_BODY = 32 * 1024 * 1024  # the Messages API's own request size limit


def make_app():
    app = web.Application(client_max_size=MAX_BODY)
    app.add_routes([*page.routes, *proxy.routes])  # proxy's last: it takes every path left
    app.cleanup_ctx.extend([lifespan, proxy.client])
    app.on_shutdown.append(page.close_events)
    return app


async def lifespan(app):
    pool = app[POOL] = Pool()
    ticking = asyncio.create_task(tick(pool))
    log(f"listening on {SOCKET} -> {proxy.UPSTREAM} "
        f"({len(load_accounts())} accounts, {len(key_names())} developer keys)")
    yield
    ticking.cancel()
    pool.tick()
    log("stopped")


async def tick(pool):
    while True:
        await asyncio.sleep(5)
        try:
            pool.tick()
        except sqlite3.Error as e:
            log(f"could not save usage: {e}")


def main():
    # Binding replaces the socket file, so a second copy would take Tailscale serve's way in from the
    # running one, and leave none when it exits.
    with socket.socket(socket.AF_UNIX) as s:
        if s.connect_ex(SOCKET) == 0:
            raise SystemExit(f"hotpocket is already running on {SOCKET}.")
    os.umask(0o077)  # owner-only files: the database holds account tokens, and the socket believes whoever connects
    # aiohttp waits shutdown_timeout twice for a request still running, which must fit in Docker's 10 s
    # stop grace for the final save to run.
    web.run_app(make_app(), path=SOCKET, shutdown_timeout=4, access_log=None, print=None)
