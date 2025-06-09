# Use a slim Python base image
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Install pip dependencies (Docker SDK and mcstatus)
RUN pip install docker mcstatus # <<< ADDED mcstatus

# --- CRITICAL ADDITION: Install Docker CLI client ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \
    "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    \"$(. /etc/os-release && echo \"$VERSION_CODENAME\")\" stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

# Copy the proxy script and the scripts directory
COPY proxy_multi.py .
COPY scripts ./scripts

# Make the scripts executable
RUN chmod +x scripts/*.sh

# The CMD instruction specifies the command to run by the container
CMD ["python", "proxy_multi.py"]
