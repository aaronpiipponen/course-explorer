"""One owner for materializing a checkout's absent working files from the tracked examples."""

from __future__ import annotations

from pathlib import Path

from course_explorer.administration import atomic_write_private_text


def copy_example_files(root: Path) -> bool:
    """Materialize every absent working template and return whether this call created ``.env``.

    A present working path is kept byte for byte. A symlinked or non-regular destination is refused
    rather than followed. A present ``config/`` directory is an ordinary directory that the missing
    files join, never one that is replaced. The created environment file is published at mode 0600.
    """
    created_environment = False
    for relative, destination_relative in template_pairs(root):
        source = root / relative
        destination = root / destination_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError(
                f"working path {destination_relative} must be a regular non-symlink file"
            )
        if destination.exists():
            continue
        payload = source.read_bytes()
        if destination_relative == ".env":
            atomic_write_private_text(destination, payload.decode("utf-8"), mode=0o600)
            created_environment = True
        else:
            destination.write_bytes(payload)
    return created_environment


def template_pairs(root: Path) -> tuple[tuple[str, str], ...]:
    """Return every tracked example template and the working path it belongs at."""
    config_root = root / "examples" / "config"
    pairs = [
        (f"examples/config/{path.name}", f"config/{path.name}")
        for path in sorted(config_root.iterdir())
        if path.is_file()
    ]
    pairs.extend(
        (
            ("examples/.env.example", ".env"),
            ("examples/docker-compose.yml", "docker-compose.yml"),
        )
    )
    return tuple(pairs)
