# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Arguments for build metadata (populated by GitHub Actions)
ARG BUILD_DATE
ARG APP_VERSION
ARG VCS_REF

# Set environment variables for application metadata
ENV APP_IMAGE_METADATA='{"version": "${APP_VERSION}", "build_date": "${BUILD_DATE}", "commit": "${VCS_REF}"}'

# Install any needed packages specified in requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# NEW: Install net-tools (for netstat) for debugging purposes.
# Ensure apt-get update is fully successful and net-tools is installed.
# It should put netstat in /usr/bin, which is usually in PATH.
RUN apt-get update -qq && apt-get install -y --no-install-recommends net-tools && rm -rf /var/lib/apt/lists/*

# Copy the application code into the container
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .
COPY send_test_packets.py .
COPY get_container_ip.py .

# Create data directory for volumes if they are used
RUN mkdir -p /app/data/nether-bridge

# Expose the default Bedrock/Java ports (these are what the proxy listens on)
EXPOSE 19132/udp
EXPOSE 19133/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 25566/udp
EXPOSE 25566/tcp

# Define entrypoint script to run your main Python application
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command for the container (can be overridden by docker-compose)
CMD []