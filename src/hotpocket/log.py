"""Log lines, on stdout for `docker compose logs`. They are also the audit log of every change."""

import datetime
import re
import time

from .tailscale import source


def log(msg):
    # Escaped, so a request path like /v1/%0A... can't forge lines in the log.
    msg = re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\x{ord(m[0]):02x}", msg)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def until_text(ts):
    """How long until ts: 1:59:59, or 1 day, 2:00:00."""
    return str(datetime.timedelta(seconds=max(0, round(ts - time.time()))))


def refused(request, why):
    """A request turned away: who sent it, what it was for, and why."""
    log(f"refused {request.method} {request.rel_url.raw_path} from {source(request)}: {why}")
