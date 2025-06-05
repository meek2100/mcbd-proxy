# Use Alpine-based Python image for a smaller footprint
FROM python:3.9-alpine # <--- CHANGED BASE IMAGE

# Set the working directory inside the container
WORKDIR /app

# Install the Docker SDK for Python
# We include common build dependencies (build-base, libffi-dev, openssl-dev)
# which are often needed for Python packages with C extensions on Alpine.
# Then we clean up the build dependencies to keep the final image small.
RUN apk add --no-cache build-base libffi-dev openssl-dev && \
    pip install docker && \
    apk del build-base libffi-dev openssl-dev && \
    rm -rf /var/cache/apk/* # Clean apk cache after installations

# --- CRITICAL ADDITION: Install Docker CLI client (Alpine version) ---
# Alpine's apk package manager can directly install docker-cli.
RUN apk add --no-cache docker-cli && \
    rm -rf /var/cache/apk/* # Clean apk cache after docker-cli install

# Copy the proxy script and the scripts directory
COPY proxy_multi.py .
COPY scripts ./scripts

# Make the scripts executable
RUN chmod +x scripts/*.sh

# The CMD instruction specifies the command to run when the container starts.
CMD ["python", "proxy_multi.py"]
