# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Install Docker CLI Client
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \"$(. /etc/os-release && echo \"$VERSION_CODENAME\")\" stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Inject the build version (git commit SHA) into the Python script.
ARG COMMIT_SHA
RUN echo "__IMAGE_VERSION__ = 'git-sha-${COMMIT_SHA:-local}'" >> /app/proxy_multi.py

# Make helper scripts executable
RUN chmod +x /app/scripts/*.sh

# Configure the health check
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "proxy_multi.py", "--healthcheck" ]

# Set the default command
CMD ["python", "proxy_multi.py"]
