# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Set working directory in the container
WORKDIR /app

# Arguments for build metadata (populated by GitHub Actions)
ARG BUILD_DATE
ARG APP_VERSION
ARG VCS_REF

# Install any needed packages specified in requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Create data directory for volumes if they are used
RUN mkdir -p /app/data/nether-bridge

# Expose the default Bedrock/Java ports
EXPOSE 19132/udp
EXPOSE 25565/udp
EXPOSE 25565/tcp
EXPOSE 8000/tcp

# Define entrypoint script to run your main Python application
ENTRYPOINT ["python", "nether_bridge.py"]

# Default command for the container
CMD []