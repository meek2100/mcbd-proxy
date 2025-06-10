#!/bin/sh
set -e

# This script validates the integrity of the main Python application
# before execution by comparing its current checksum against the
# checksum generated at build time.

APP_SCRIPT="/app/proxy_multi.py"
BUILD_CHECKSUM_FILE="/app/checksum.sha256"

# Read the checksum that was stored when the image was built.
# The 'cut' command is used to get only the checksum hash itself.
BUILD_CHECKSUM=$(cut -d' ' -f1 "$BUILD_CHECKSUM_FILE")

# Calculate the checksum of the script that is currently in the container.
LIVE_CHECKSUM=$(sha256sum "$APP_SCRIPT" | cut -d' ' -f1)

# Compare the two checksums.
if [ "$BUILD_CHECKSUM" = "$LIVE_CHECKSUM" ]; then
  # Checksums match: We are running the unmodified script from the image.
  echo "Validation OK: Running script from Docker image."
else
  # Checksums do NOT match: The script has been replaced by a volume mount.
  echo "Validation MISMATCH: Running script from a mounted volume (local override)."
fi

# Regardless of the outcome, proceed to execute the main Python application.
echo "----------------------------------------------------"
exec python "$APP_SCRIPT"
