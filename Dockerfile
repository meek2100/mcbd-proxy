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

# Make the start/stop shell scripts executable
RUN chmod +x /app/scripts/start-server.sh /app/scripts/stop-server.sh

# --- HEALTHCHECK ---
# Execute the health check logic directly within the Python script.
# This is the most reliable method as it has no external dependencies.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD [ "python", "proxy_multi.py", "--healthcheck" ]

# Command to run the application
CMD ["python", "proxy_multi.py"]
