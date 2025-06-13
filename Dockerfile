# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Install necessary packages, including gosu for user switching
RUN apt-get update && apt-get install -y gosu && rm -rf /var/lib/apt/lists/*

# Set working directory in the container
WORKDIR /app

# Create a non-privileged user. We will handle group permissions in the entrypoint.
RUN adduser --system --no-create-home nonroot

# Create a writable directory for runtime files
RUN mkdir -p /run/app && chown nonroot:nonroot /run/app

# Copy application and entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Change ownership of the app directory
RUN chown -R nonroot:nonroot /app

# Expose ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Use the new entrypoint. It will execute the CMD as the 'nonroot' user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# The command that the entrypoint will run
CMD ["python", "nether_bridge.py"]