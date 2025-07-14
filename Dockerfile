# Dockerfile
# --- Base Stage ---
# Use an official lightweight Python image.
# Using a specific version tag ensures builds are reproducible.
FROM python:3.11-slim as base

# Set environment variables to prevent Python from writing .pyc files and to
# run in unbuffered mode, which is better for logging.
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# --- Builder Stage ---
# This stage installs the Python dependencies.
FROM base as builder

# Set the working directory
WORKDIR /app

# Install build tools
RUN pip install --no-cache-dir --upgrade pip wheel

# Copy only the necessary files for dependency installation
# This leverages Docker's layer caching. If these files don't change,
# Docker won't re-run the pip install step.
COPY pyproject.toml poetry.lock* ./

# Install application dependencies
# Using --no-cache-dir keeps the image size smaller.
RUN pip install --no-cache-dir .

# --- Final Stage ---
# This is the final, clean image that will be used to run the application.
FROM base as final

# Create a non-root user and group for security
RUN addgroup --system app && adduser --system --group app

# Set the working directory
WORKDIR /app

# Copy the installed dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application source code
COPY . .

# Change ownership of the app directory to the non-root user
RUN chown -R app:app /app

# Switch to the non-root user
USER app

# Expose the default Minecraft Java, Bedrock, and metrics ports.
# The Bedrock port requires UDP, which is specified here.
EXPOSE 25565
EXPOSE 19132/udp
EXPOSE 8000

# Define the entry point for the container. This command will be run when
# the container starts.
ENTRYPOINT ["python", "-m", "main"]