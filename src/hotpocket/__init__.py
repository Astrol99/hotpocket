"""hotpocket: one Claude Code endpoint for the team, backed by a pool of Claude accounts.

    server     starts it, on the unix socket Tailscale serve reaches it through
    proxy      sends Claude Code's API calls on through an account from the pool
    pool       each account's usage and blocks, and which one takes a request
    usage      what the API's replies say about limits and tokens
    page       the status page (its files are in static/) and the edits made on it
    tailscale  who is asking
    store      the database
    log        log lines
"""
