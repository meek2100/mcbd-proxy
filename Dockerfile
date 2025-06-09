# Use an official Python runtime as a parent image 
FROM python:3.9-slim

# --- CRITICAL ADDITION: Install Docker CLI client ---
# This is required so the container can execute 'docker start' and 'docker stop' commands.
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

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt 
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container 
COPY . .

# Make the start/stop shell scripts executable
RUN chmod +x /app/scripts/start-server.sh /app/scripts/stop-server.sh

# --- HEALTHCHECK ---
# Execute the health check logic directly within the Python script. 
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "proxy_multi.py", "--healthcheck" ] 

# Command to run the application
CMD ["python", "proxy_multi.py"]
