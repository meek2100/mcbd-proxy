# tests/docker_utils.py

import sys
import time

import docker


def ensure_container_stopped(container_name: str, timeout: int = 60):
    """Connects to Docker via socket and ensures a container is stopped."""
    print(f"--- Verifying container '{container_name}' is stopped... ---")
    client = None
    try:
        client = docker.from_env()
        container = client.containers.get(container_name)

        if container.status == "running":
            print("--- Container is running. Sending stop command... ---")
            container.stop(timeout=20)

            # Wait for the container to report as exited
            start_time = time.time()
            while time.time() - start_time < timeout:
                container.reload()
                if container.status != "running":
                    print(f"--- Container '{container_name}' confirmed stopped. ---")
                    return
                time.sleep(1)
            raise TimeoutError(f"Container {container_name} did not stop in time.")
        else:
            print(f"--- Container '{container_name}' is already stopped. ---")

    except docker.errors.NotFound:
        print(f"--- Container '{container_name}' not found, which is OK. ---")
        # If the container doesn't exist, it's considered stopped.
        pass
    except Exception as e:
        print(f"CRITICAL: An error occurred in Docker pre-check: {e}", file=sys.stderr)
        # Re-raise to halt the test execution
        raise
    finally:
        if client:
            client.close()
