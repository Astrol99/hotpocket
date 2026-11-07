"""Storage, logging and serving: what's on disk, the log, and the unix socket."""

import asyncio
import sys

import aiohttp
import pytest

from hotpocket import server, store
from hotpocket.log import log


def test_keys_are_stored_only_as_hashes(data):
    key = store.new_key("alice")
    stored = b"".join(f.read_bytes() for f in data.iterdir())
    assert key.encode() not in stored and store.key_hash(key).encode() in stored


def test_log_lines_cannot_be_forged(caplog):
    # Say an upstream error page: its text is logged, and mustn't start lines of its own.
    log.warning("refused (429: <html>\n2026-01-01 00:00:00 INFO admin evil: removed developer bob)")
    assert caplog.records[-1].getMessage() == "refused (429: <html>\\x0a2026-01-01 00:00:00 INFO admin evil: removed developer bob)"


async def test_it_serves_on_its_socket_with_owner_only_files(tmp_path, monkeypatch):
    sock = tmp_path / "hotpocket.sock"
    monkeypatch.setenv("HOTPOCKET_DATA", str(tmp_path))
    monkeypatch.setenv("HOTPOCKET_SOCKET", str(sock))
    running = await asyncio.create_subprocess_exec(sys.executable, "-c", "from hotpocket.server import main; main()")
    try:
        async with asyncio.timeout(10):
            while not sock.exists():
                await asyncio.sleep(0.05)
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(str(sock))) as http:
            assert (await http.get("http://hotpocket/api/hello")).status == 200
        assert all(f.stat().st_mode & 0o077 == 0 for f in tmp_path.iterdir())  # the database and the socket
        monkeypatch.setattr(server, "SOCKET", str(sock))
        with pytest.raises(SystemExit, match="already running"):  # a second copy
            server.main()
    finally:
        running.terminate()
        await running.wait()
