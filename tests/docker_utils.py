# tests/docker_utils.py

import asyncio
import sys
import time

import aiodocker
import aiodocker.exceptions


async def ensure_container_stopped(container_name: str, timeout: int = 60):
    """
    Connects to Docker via aiodocker and ensures a container is stopped.
    This function is asynchronous.
    """
    print(f"--- Verifying container '{container_name}' is stopped... ---")
    client = None
    try:
        # It's generally better to pass the client, but for simplicity
        # with how load_tester uses this, we instantiate it here.
        client = aiodocker.Docker()
        container = await client.containers.get(container_name)

        info = await container.show()
        if info["State"]["Running"]:
            print("--- Container is running. Sending stop command... ---")
            await container.stop(timeout=20)

            # Wait for the container to report as exited
            start_time = time.time()
            while time.time() - start_time < timeout:
                info = await container.show()
                if not info["State"]["Running"]:
                    print(f"--- Container '{container_name}' confirmed stopped. ---")
                    return
                await asyncio.sleep(1)
            raise TimeoutError(f"Container {container_name} did not stop in time.")
        else:
            print(f"--- Container '{container_name}' is already stopped. ---")

    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            print(f"--- Container '{container_name}' not found, which is OK. ---")
            pass  # If the container doesn't exist, it's considered stopped.
        else:
            print(
                f"CRITICAL: An error occurred in Docker pre-check: {e}",
                file=sys.stderr,
            )
            # Re-raise to halt the test execution
            raise
    except Exception as e:
        print(f"CRITICAL: An unexpected error occurred: {e}", file=sys.stderr)
        raise
    finally:
        if client:
            await client.close()
