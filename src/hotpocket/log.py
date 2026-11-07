"""The "hotpocket" logger, which server.main prints with each line's time and level, for `docker compose
logs`. It is also the audit log of every change."""

import logging
import re

from .tailscale import source

log = logging.getLogger("hotpocket")
log.setLevel(logging.INFO)


def escape(record):
    # Every line escaped, so a request path like /v1/%0A... can't forge lines in the log.
    record.msg, record.args = re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\x{ord(m[0]):02x}", record.getMessage()), ()
    return True


log.addFilter(escape)


def refused(request, why):
    """A request turned away: who sent it, what it was for, and why."""
    log.warning(f"refused {request.method} {request.rel_url.raw_path} from {source(request)}: {why}")
