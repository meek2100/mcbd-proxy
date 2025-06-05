# Use a slim Python base image
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Install pip dependencies (Docker SDK)
RUN pip install docker

# --- CRITICAL ADDITION: Install Docker CLI client ---
# Install necessary packages for Docker CLI (ca-certificates, curl, gnupg)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg && \
    # Add Docker's official GPG key
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    # Add the Docker repository to Apt sources
    echo \
    "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    \"$(. /etc/os-release && echo \"$VERSION_CODENAME\")\" stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    # Install Docker CLI
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*
# --- END CRITICAL ADDITION ---

# Copy the proxy script and the scripts directory
COPY proxy_multi.py .
COPY scripts ./scripts

# Make the scripts executable
RUN chmod +x scripts/*.sh

# The CMD instruction specifies the command to run when the container starts.
CMD ["python", "proxy_multi.py"]
