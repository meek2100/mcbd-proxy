#!/bin/bash

# env_check.sh
# This script provides detailed information about the Jules test environment.

echo "--- Environment Check Script Started ---"
echo "Timestamp: $(date)"
echo "Working directory: $(pwd)"
echo "User: $(whoami)"
echo "User ID and Groups: $(id)"

echo ""
echo "--- Python Environment Details ---"
echo "Default Python executable: $(which python)"
echo "Default Python version: $(python --version)"
echo "Poetry environment Python executable: $(poetry run which python)"
echo "Poetry environment Python version: $(poetry run python --version)"
echo "Poetry virtual environment path: $(poetry env info -p)"

echo ""
echo "--- Installed Python Packages (from Poetry environment) ---"
poetry run pip freeze

echo ""
echo "--- Python sys.path (where Python looks for modules in Poetry env) ---"
poetry run python -c "import sys; print(sys.path)"

echo ""
echo "--- Python Module Import Tests ---"
echo "Testing import of 'aiodocker':"
poetry run python -c "import aiodocker; print('aiodocker imported successfully.')" || echo "aiodocker import FAILED."
echo "Testing import of 'docker':"
poetry run python -c "import docker; print('docker imported successfully.')" || echo "docker import FAILED."
echo "Testing import of 'mcstatus':"
poetry run python -c "import mcstatus; print('mcstatus imported successfully.')" || echo "mcstatus import FAILED."
echo "Testing import of 'pytest':"
poetry run python -c "import pytest; print('pytest imported successfully.')" || echo "pytest import FAILED."
echo "Testing import of 'ruff':"
poetry run python -c "import ruff; print('ruff imported successfully.')" || echo "ruff import FAILED."

echo ""
echo "--- Docker Environment Details ---"
echo "Docker version: $(sudo docker version --format '{{.Server.Version}}')"
echo "Docker Compose version: $(sudo docker compose version --short)"
echo "Docker Info (summary):"
sudo docker info --format '{{.OperatingSystem}} {{.KernelVersion}} {{.NCPU}} CPUs {{.MemTotal}} Total Memory'
echo "Docker daemon status (sudo docker ps -a):"
sudo docker ps -a
echo "Docker network list (sudo docker network ls):"
sudo docker network ls

echo ""
echo "--- System Resource Details ---"
echo "Disk Usage (df -h):"
df -h
echo "Memory Usage (free -h):"
free -h
echo "Network Interfaces (ip a):"
ip a

echo ""
echo "--- Environment Check Script Finished ---"