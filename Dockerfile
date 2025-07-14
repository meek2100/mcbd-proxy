# ---- Builder Stage ----
# This stage installs all project dependencies, including for development.
FROM python:3.11-slim-bookworm as builder

# Create the working directory first, owned by root initially.
WORKDIR /app

# Create a non-root user for security.
RUN useradd --create-home --shell /bin/bash appuser

# Set ownership of the app directory BEFORE switching to the user.
RUN chown appuser:appuser /app

# Now, switch to the non-root user.
USER appuser

# Create and activate a virtual environment. This will now succeed.
ENV VIRTUAL_ENV=/app/.venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install all dependencies from pyproject.toml.
COPY pyproject.toml poetry.lock* ./
RUN pip install --no-cache-dir .[dev]


# ---- Testing Stage ----
# This stage is specifically for running tests from within a container.
FROM builder as testing

# Copy the application source code into the testing stage.
COPY . /app
USER appuser


# ---- Final Stage ----
# This is the lean, production-ready image.
FROM python:3.11-slim-bookworm as final

WORKDIR /app
RUN useradd --create-home --shell /bin/bash appuser

# Install gosu for easy user-switching.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*

# Copy the installed dependencies and application code.
COPY --from=builder /app/.venv /app/.venv
COPY . /app

# Set correct ownership for the entire application directory.
RUN chown -R appuser:appuser /app

USER appuser
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 25565 19132

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]