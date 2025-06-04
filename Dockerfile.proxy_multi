# Use a slim Python base image
FROM python:3.9-slim-buster

# Set the working directory inside the container
WORKDIR /app

# Install the Docker SDK for Python
RUN pip install docker

# Copy the proxy script and the scripts directory from the build context (your repo)
COPY proxy_multi.py .
COPY scripts/ scripts/ # Copies the entire 'scripts' directory into /app/scripts

# Make the scripts executable
RUN chmod +x scripts/*.sh

# The CMD instruction specifies the command to run when the container starts.
# It expects proxy_multi.py to be directly in /app.
CMD ["python", "proxy_multi.py"]
