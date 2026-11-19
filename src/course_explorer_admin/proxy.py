"""One owner of the machine-wide public site proxy.

The proxy is one Compose project in the machine-wide shared directory, and one top Caddyfile imports
one site file per published instance. ``publish`` writes one instance's site file, starts the
project and reloads the running Caddy. That site file carries the proxy's configured bind address
when one is set. ``unpublish`` removes that one site file and reloads a running project. A reload
that fails leaves every site Caddy already serves in place, because Caddy keeps the configuration
it loaded last.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from course_explorer.config import load_administrative_dataset_settings
from course_explorer_admin import compose, mechanics

# The proxy project's directory inside the machine-wide shared directory.
PROXY_DIRECTORY = "proxy"
# The directory holding one site file per published instance.
SITES_DIRECTORY = "sites"
# The top Caddyfile and the Compose file of the proxy project.
CADDYFILE = "Caddyfile"
COMPOSE_FILE = "docker-compose.yml"
# Caddy's own administration endpoint inside the proxy container, which answers no host port.
ADMIN_ADDRESS = "unix//tmp/caddy-admin.sock"


def proxy_root(shared_root: Path) -> Path:
    """Return the proxy project's directory inside the machine-wide shared directory."""
    return shared_root / PROXY_DIRECTORY


def compose_file(shared_root: Path) -> Path:
    """Return the proxy project's Compose file."""
    return proxy_root(shared_root) / COMPOSE_FILE


def site_file(shared_root: Path, instance_key: str) -> Path:
    """Return one published instance's site file."""
    return proxy_root(shared_root) / SITES_DIRECTORY / f"{instance_key}.caddy"


def render_site(*, domain: str, host: str, port: int, bind_address: str | None) -> str:
    """Render one instance's site file for its domain, loopback site target and optional bind.

    ``bind_address`` adds one ``bind`` line, so the proxy listens on that host address alone for
    this site. ``None`` omits the line, and the proxy listens on every host address.
    """
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    bind = f"\tbind {bind_address}\n" if bind_address is not None else ""
    return (
        "# Public site for the configured domain, forwarded to this instance's loopback site port.\n"
        f"https://{domain} {{\n"
        f"{bind}"
        f"\treverse_proxy {target}\n"
        "}\n"
    )


def _create_directory(path: Path) -> None:
    """Create one proxy directory readable by the container whatever the invoking umask is."""
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.chmod(0o755)


def _refuse_irregular(path: Path) -> None:
    """Refuse a symlinked or non-regular proxy path rather than following it."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"proxy path {path.name} must be a regular non-symlink file")


def _materialize(root: Path, checkout_root: Path) -> None:
    """Create the proxy directory with its site directory and copy the two absent project files."""
    template_root = checkout_root / "examples" / "deploy" / "proxy"
    _create_directory(root)
    for name in (CADDYFILE, COMPOSE_FILE):
        destination = root / name
        _refuse_irregular(destination)
        if not destination.exists():
            destination.write_bytes((template_root / name).read_bytes())
            destination.chmod(0o644)
    _create_directory(root / SITES_DIRECTORY)


def _compose(
    runner: mechanics.ProcessRunner, root: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes]:
    """Run one Compose command in the proxy project and return its result.

    The command names the project file at ``root``. That file carries its own project name, and
    every relative mount source resolves against the file's directory.
    """
    return runner.run(
        (
            *compose.docker_prefix(non_interactive=True),
            "compose",
            "--file",
            str(root / COMPOSE_FILE),
            *arguments,
        ),
        cwd=root,
        timeout=load_administrative_dataset_settings().database_operation_timeout_seconds,
    )


def _reload(runner: mechanics.ProcessRunner, root: Path) -> None:
    """Reload the running Caddy from its top configuration and the site files it imports."""
    _compose(
        runner,
        root,
        "exec",
        "-T",
        "proxy",
        "caddy",
        "reload",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
        "--address",
        ADMIN_ADDRESS,
    )


def publish(
    runner: mechanics.ProcessRunner,
    *,
    checkout_root: Path,
    shared_root: Path,
    instance_key: str,
    site_address: str,
    host: str,
    port: int,
    bind_address: str | None,
) -> None:
    """Write one instance's site file, start the proxy project and reload the running Caddy.

    The two project files are copied from the checkout's templates only while they are absent, and a
    present file is kept byte for byte. The site file is written from the configured address, the
    instance's loopback site target and the optional bind address on every call, so a changed value
    reaches the proxy. The reload reads the running container's configuration, so a refused reload
    leaves the sites Caddy loaded before it in service.
    """
    domain = urlsplit(site_address).hostname
    if domain is None:
        raise ValueError("the site address must name a host")
    root = proxy_root(shared_root)
    _materialize(root, checkout_root)
    destination = site_file(shared_root, instance_key)
    _refuse_irregular(destination)
    destination.write_text(
        render_site(domain=domain, host=host, port=port, bind_address=bind_address),
        encoding="utf-8",
    )
    destination.chmod(0o644)
    _compose(runner, root, "up", "-d", "--wait", "proxy")
    _reload(runner, root)


def unpublish(runner: mechanics.ProcessRunner, *, shared_root: Path, instance_key: str) -> None:
    """Remove one instance's site file and reload a running proxy.

    The site file is the instance's one publication record, so an absent file is a no-op and
    removing it leaves every other site in place. A stopped or absent project serves nothing, so
    only a running project reloads.
    """
    destination = site_file(shared_root, instance_key)
    _refuse_irregular(destination)
    destination.unlink(missing_ok=True)
    if running(runner, shared_root=shared_root):
        _reload(runner, proxy_root(shared_root))


def running(runner: mechanics.ProcessRunner, *, shared_root: Path) -> bool:
    """Report whether the proxy container runs, and ``False`` while the project file is absent."""
    if not compose_file(shared_root).is_file():
        return False
    output = _compose(runner, proxy_root(shared_root), "ps", "--status", "running", "--services")
    return "proxy" in output.stdout.decode().split()


def _decode_local_address(encoded: str) -> str:
    """Decode one listen-table address that the kernel prints one host-order word at a time."""
    octets = b"".join(
        bytes.fromhex(encoded[index : index + 8])[::-1] for index in range(0, len(encoded), 8)
    )
    family = socket.AF_INET if len(octets) == 4 else socket.AF_INET6
    return socket.inet_ntop(family, octets)


def listening_sockets() -> frozenset[tuple[str, int]]:
    """Return every local address and port in the listen state in the host network namespace.

    The proxy container shares the host network namespace, so the kernel's tables are the host's,
    and reading them needs no privilege to bind a port below 1024. Each entry is one decoded address
    and its port, so a caller can tell a specific listener from a wildcard one.
    """
    sockets: set[tuple[str, int]] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        # A host with IPv6 disabled has no tcp6 table.
        if not table.exists():
            continue
        for line in table.read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A":
                encoded, _, port = fields[1].rpartition(":")
                sockets.add((_decode_local_address(encoded), int(port, 16)))
    return frozenset(sockets)
