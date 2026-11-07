FROM python:3.13-slim-trixie
WORKDIR /app
COPY . .
# uv is only borrowed for the build, and its download cache stays in a build cache: neither ships.
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.3,source=/uv,target=/bin/uv --mount=type=cache,target=/root/.cache/uv \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy uv sync --frozen --no-dev --no-editable --compile-bytecode \
 && useradd --system --uid 10001 hotpocket && mkdir /data /run/hotpocket && chown hotpocket /data /run/hotpocket
ENV PATH=/app/.venv/bin:$PATH
USER hotpocket
CMD ["hotpocket"]
