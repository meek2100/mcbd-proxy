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

# Install all dependencies from pyproject.toml.
COPY --chown=appuser:appuser pyproject.toml ./
RUN pip install --no-cache-dir .[dev]

# ---- Testing Stage (Restored) ----
# This stage is specifically for running tests from within a container,
# ensuring a consistent test environment everywhere.
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

# Copy the installed dependencies and the application code.
COPY --from=builder /app/.venv /app/.venv
COPY --chown=appuser:appuser . /app

# Set correct ownership for the entire application directory.
RUN chown -R appuser:appuser /app

USER appuser
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 25565 19132

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]