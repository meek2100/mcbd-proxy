import docker
import sys

container_name = sys.argv[1] if len(sys.argv) > 1 else None
network_name = sys.argv[2] if len(sys.argv) > 2 else None

if not container_name or not network_name:
    print("Usage: python get_container_ip.py <container_name> <network_name>")
    sys.exit(1)

try:
    client = docker.from_env()
    container = client.containers.get(container_name)
    
    networks = container.attrs['NetworkSettings']['Networks']
    if network_name in networks:
        ip_address = networks[network_name]['IPAddress']
        print(ip_address)
    else:
        print(f"Error: Container '{container_name}' not found on network '{network_name}'.")
        sys.exit(1)
except docker.errors.NotFound:
    print(f"Error: Container '{container_name}' not found.")
    sys.exit(1)
except Exception as e:
    print(f"Error: Failed to get IP for container '{container_name}': {e}")
    sys.exit(1)