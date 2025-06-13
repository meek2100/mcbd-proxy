# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Arguments for build metadata (populated by GitHub Actions)
ARG BUILD_DATE
ARG APP_VERSION
ARG VCS_REF

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into the container
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Expose the necessary ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Define the entrypoint to run your main Python application
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command for the container
CMD []