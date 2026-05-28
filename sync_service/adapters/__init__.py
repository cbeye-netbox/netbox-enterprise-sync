from ..config import EndpointConfig
from .base import Endpoint
from .local import LocalEndpoint
from .ssh import SSHEndpoint


def make_endpoint(config: EndpointConfig) -> Endpoint:
    if config.transport == "local":
        return LocalEndpoint(config)
    if config.transport == "ssh":
        return SSHEndpoint(config)
    raise ValueError(f"unknown transport: {config.transport}")


__all__ = ["Endpoint", "LocalEndpoint", "SSHEndpoint", "make_endpoint"]
