import asyncio
import random
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv


@pytest_asyncio.fixture(scope="session")
async def docker_compose_up(pytestconfig):
    """
    Manages the Docker Compose test environment asynchronously.
    """
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path)

    project_name = f"netherbridge_test_{random.randint(1, 1000)}"
    compose_file = str(Path(__file__).parent / "docker-compose.tests.yml")

    up_command = [
        "docker",
        "compose",
        "-p",
        project_name,
        "-f",
        compose_file,
        "up",
        "--build",
        "--force-recreate",
        "-d",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *up_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            pytest.fail(
                f"Docker Compose failed to start:\n{stderr.decode()}", pytrace=False
            )

        # Wait a few seconds for services to stabilize
        await asyncio.sleep(5)
        yield

    finally:
        down_command = [
            "docker",
            "compose",
            "-p",
            project_name,
            "-f",
            compose_file,
            "down",
            "-v",
        ]
        process = await asyncio.create_subprocess_exec(
            *down_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
