# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Install dependencies: Docker CLI and coreutils (for sha256sum, tr)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    coreutils \
    ca-certificates \
    curl \
    gnupg && \
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

# Create the build-time checksum file, ignoring line-ending differences.
RUN tr -d '\r' < /app/proxy_multi.py | sha256sum | cut -d' ' -f1 > /app/checksum.sha256

# Make all helper scripts executable
RUN chmod +x /app/scripts/*.sh

# Configure the health check (this points to the Python script's internal check)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "proxy_multi.py", "--healthcheck" ]

# Set the default command to run our validation script first
CMD ["/app/scripts/validate.sh"]
