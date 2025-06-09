# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container
COPY . .

# Make the shell scripts executable
RUN chmod +x /app/scripts/*.sh

# --- HEALTHCHECK ---
# Use the custom healthcheck script.
# This script implements a two-stage check for configuration and liveness.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/app/scripts/healthcheck.sh"]

# Command to run the application
CMD ["python", "proxy_multi.py"]
