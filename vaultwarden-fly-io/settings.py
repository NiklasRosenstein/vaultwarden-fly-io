"""Environment variable parsing shared by the entrypoint and the backup worker."""

from __future__ import annotations

import os


class ConfigError(ValueError):
    """A configuration problem with a message that is safe to log."""


def env(name: str, default: str = "") -> str:
    # Like the shell's ${NAME:-default}: an empty value counts as unset.
    return os.environ.get(name) or default


def required(name: str, *, allow_empty: bool = False) -> str:
    value = os.environ.get(name)
    if value is None or (not value and not allow_empty):
        raise ConfigError(f'missing expected environment variable "{name}"')
    return value


def flag(name: str, default: bool) -> bool:
    value = env(name, "true" if default else "false")
    if value not in ("true", "false"):
        raise ConfigError(f'{name} must be "true" or "false", got "{value}"')
    return value == "true"


def integer(name: str, default: int, *, minimum: int = 0) -> int:
    value = env(name, str(default))
    if not value.isdigit() or int(value) < minimum:
        raise ConfigError(
            f'{name} must be an integer of at least {minimum}, got "{value}"'
        )
    return int(value)


def positive_int(name: str, default: int) -> int:
    return integer(name, default, minimum=1)
