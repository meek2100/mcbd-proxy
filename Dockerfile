# ---- Builder Stage ----
# This stage installs all project dependencies, including for development.
FROM python:3.11-slim-bookworm as builder

WORKDIR /app

# Create a non-root user for security.
RUN useradd --create-home --shell /bin/bash naeus
USER naeus

# Set up and activate a virtual environment.
ENV VIRTUAL_ENV=/app/.venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install all dependencies from pyproject.toml.
COPY --chown=naeus:naeus pyproject.toml ./
RUN pip install --no-cache-dir .[dev]

# ---- Testing Stage ----
# This stage is specifically for running tests from within a container.
FROM builder as testing

# Copy the application source code into the testing stage.
COPY --chown=naeus:naeus . /app
USER naeus


# ---- Final Stage ----
# This is the lean, production-ready image.
FROM python:3.11-slim-bookworm as final

WORKDIR /app

# Create a non-root user.
RUN useradd --create-home --shell /bin/bash naeus

# Install gosu for easy user-switching.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*

# Copy the installed dependencies and application code.
COPY --from=builder /app/.venv /app/.venv
COPY --chown=naeus:naeus . /app

# FIX: Make the entrypoint script executable and ensure correct ownership.
RUN chown -R naeus:naeus /app && chmod +x /app/entrypoint.sh

USER naeus
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 25565 19132

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]