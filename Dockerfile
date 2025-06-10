# Use an official Python runtime as a parent image
FROM python:3.9-slim

# --- Install Docker CLI Client ---
# This is required so the container can execute 'docker start' and 'docker stop' commands
# by communicating with the host's Docker daemon via the mounted docker.sock.
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

# Set the working directory in the container
WORKDIR /app

# Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# --- Inject a version variable into the script within the image ---
# This allows the script to identify itself as the image version.
ARG BUILD_DATE
RUN cat <<EOF >> /app/proxy_multi.py
__IMAGE_VERSION__ = "Image-Build-Date: ${BUILD_DATE:-unset}"
EOF

# Make the helper shell scripts executable
RUN chmod +x /app/scripts/*.sh

# --- HEALTHCHECK ---
# Executes the health check logic within the Python application itself.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "proxy_multi.py", "--healthcheck" ]

# Command to run the application
CMD ["python", "proxy_multi.py"]
