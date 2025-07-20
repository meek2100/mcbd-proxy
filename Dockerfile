# Dockerfile
# Stage 0: Poetry-base - A dedicated stage to install Poetry itself.
FROM python:3.11-slim-bookworm AS poetry-base
ENV POETRY_HOME="/opt/poetry"
ENV POETRY_VIRTUALENVS_CREATE=false
# Install curl, which is needed for the Poetry installer.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="$POETRY_HOME/bin:$PATH"

# Stage 1: Base - Installs production dependencies.
FROM poetry-base AS base
WORKDIR /app
COPY pyproject.toml poetry.lock ./
# Regenerate lock file if inconsistent, then sync dependencies.
RUN poetry lock --no-interaction
RUN poetry sync --no-root --without dev --no-interaction

# Stage 2: Builder - A complete copy of the source code.
FROM python:3.11-slim-bookworm AS builder
WORKDIR /app
COPY . .

# Stage 3: Testing - Includes dev dependencies for running tests.
FROM base AS testing
WORKDIR /app
COPY --from=poetry-base ${POETRY_HOME} ${POETRY_HOME}
COPY --from=builder /app /app
# Regenerate lock file if inconsistent, then sync all dependencies.
RUN poetry lock --no-interaction
RUN poetry sync --no-root --no-interaction
# Install system packages needed by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu passwd \
  && rm -rf /var/lib/apt/lists/*
RUN adduser --system --no-create-home naeus && \
  chown -R naeus:nogroup /app && \
  chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/bin/bash"]

# Stage 4: Final Production Image - Assembled for a lean image.
FROM python:3.11-slim-bookworm AS final
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gosu procps \
  && rm -rf /var/lib/apt/lists/*
RUN adduser --system --no-create-home naeus

# Copy artifacts from previous stages.
COPY --from=base /usr/local/lib/python3.11/site-packages \
  /usr/local/lib/python3.11/site-packages
COPY --from=builder /app/entrypoint.sh /usr/local/bin/

# Copy application code
COPY --from=builder --chown=naeus:nogroup /app/main.py .
COPY --from=builder --chown=naeus:nogroup /app/proxy.py .
COPY --from=builder --chown=naeus:nogroup /app/config.py .
COPY --from=builder --chown=naeus:nogroup /app/docker_manager.py .
COPY --from=builder --chown=naeus:nogroup /app/metrics.py .
COPY --from=builder --chown=naeus:nogroup /app/examples/ ./examples/

RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R naeus:nogroup /app

EXPOSE 19132/udp 25565/udp 25565/tcp 8000/tcp

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["gosu", "naeus", "python", "main.py", "--healthcheck"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "main.py"]