# ---- Builder Stage ----
# This stage installs all project dependencies, including for development.
FROM python:3.11-slim-bookworm as builder

WORKDIR /app

# Create a non-root user for better security.
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# Set up and activate a virtual environment.
ENV VIRTUAL_ENV=/app/.venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install all dependencies, including development and testing tools.
COPY --chown=appuser:appuser pyproject.toml poetry.lock* ./
RUN pip install --no-cache-dir .[dev]

# ---- Testing Stage ----
# This stage is specifically for running integration tests.
FROM builder as testing

# Copy the application source code into the testing stage.
COPY --chown=appuser:appuser . /app
USER appuser

# ---- Final Stage ----
# This is the lean, production-ready image.
FROM python:3.11-slim-bookworm as final

WORKDIR /app

# Create a non-root user.
RUN useradd --create-home --shell /bin/bash appuser

# Install gosu for easy user-switching in the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*

# Copy the installed dependencies from the builder stage.
COPY --from=builder /app/.venv /app/.venv
# Copy the application code.
COPY --chown=appuser:appuser . /app

# Set correct ownership for the entire application directory.
RUN chown -R appuser:appuser /app

USER appuser
ENV PATH="/app/.venv/bin:$PATH"

# Expose the default Minecraft server ports.
EXPOSE 25565 19132

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]