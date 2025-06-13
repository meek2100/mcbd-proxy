# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Install gosu for privilege dropping and ca-certificates for networking
RUN apt-get update && apt-get install -y gosu ca-certificates && rm -rf /var/lib/apt/lists/*

# Set working directory in the container
WORKDIR /app

# Create the non-privileged user and its primary group
RUN adduser --system --no-create-home --group nonroot

# Create a writable directory for the application's runtime files
RUN mkdir -p /run/app && chown nonroot:nonroot /run/app

# Copy the new entrypoint script and make it executable
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy application code first
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Change ownership of the app directory and its contents to the non-root user
RUN chown -R nonroot:nonroot /app

# Expose ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Use the new entrypoint. It will handle permissions and then run the CMD.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# The command that the entrypoint will run as the 'nonroot' user
CMD ["python", "nether_bridge.py"]