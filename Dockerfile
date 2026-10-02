FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# git fetches the pinned tradingagents release named in pyproject.toml.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# The same UID as the host user, so a bind-mounted ~/.tradingagents stays writable by both.
ARG UID=1000
RUN useradd --create-home --uid "${UID}" appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000
# 0.0.0.0 inside the container; docker-compose.yml publishes the port on 127.0.0.1 only.
# Publishing it wider requires TRADINGAGENTS_WEB_TOKEN.
CMD ["tradingagents-web", "--host", "0.0.0.0", "--port", "8000", "--allow-unauthenticated"]
