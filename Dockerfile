# Use an official Python runtime as a parent image
FROM python:3.10-slim-buster

# Install gosu for privilege dropping and ca-certificates for networking
RUN apt-get update && apt-get install -y gosu ca-certificates && rm -rf /var/lib/apt/lists/*

# Set working directory in the container
WORKDIR /app

# Create the non-privileged user
RUN adduser --system --no-create-home --group nonroot

# Create a writable directory for the application's runtime files
RUN mkdir -p /run/app && chown nonroot:nonroot /run/app

# Copy the new entrypoint script and make it executable
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and set ownership
COPY nether_bridge.py .
COPY settings.json .
COPY servers.json .
RUN chown -R nonroot:nonroot /app

# Expose ports
EXPOSE 19132/udp
EXPOSE 25565/tcp
EXPOSE 25565/udp

# Set the entrypoint. It will handle permissions and then run the CMD.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Define the default command to be executed by the entrypoint
CMD ["python", "nether_bridge.py"]