# Use Alpine-based Python image for a smaller footprint
FROM python:3.9-alpine 

# Set the working directory inside the container
WORKDIR /app

# Install the Docker SDK for Python
RUN apk add --no-cache build-base libffi-dev openssl-dev && \
    pip install docker && \
    apk del build-base libffi-dev openssl-dev && \
    rm -rf /var/cache/apk/*

# Install Docker CLI client (Alpine version)
RUN apk add --no-cache docker-cli && \
    rm -rf /var/cache/apk/*

# Copy the proxy script and the scripts directory
COPY proxy_multi.py /app/
COPY scripts/ /app/scripts/

# Make the scripts executable
RUN chmod +x /app/scripts/*.sh

# The CMD instruction specifies the command to run when the container starts.
CMD ["python", "/app/proxy_multi.py"]
