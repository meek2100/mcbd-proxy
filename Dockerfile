# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
# (Create a requirements.txt file with the content below)
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# --- HEALTHCHECK ---
# This command checks if the heartbeat file has been updated in the last minute.
# --interval: Run check every 30s.
# --timeout: The check must complete in 10s.
# --start-period: Grace period of 60s after container starts.
# --retries: Mark as unhealthy after 3 consecutive failures.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD find /tmp/proxy_heartbeat -mmin -1 | grep -q . || exit 1

# Command to run the application
CMD ["python", "proxy_multi.py"]
