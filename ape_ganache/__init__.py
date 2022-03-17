"""
Ape network provider plugin for Ganache (Ethereum development framework and network
implementation written in Node.js).
"""

from ape import plugins
from ape.api.networks import LOCAL_NETWORK_NAME

from .providers import (
    GanacheMainnetForkProvider,
    GanacheNetworkConfig,
    GanacheProvider,
    GanacheProviderError,
)


@plugins.register(plugins.Config)
def config_class():
    return GanacheNetworkConfig


@plugins.register(plugins.ProviderPlugin)
def providers():
    yield "ethereum", LOCAL_NETWORK_NAME, GanacheProvider
    yield "ethereum", "mainnet-fork", GanacheMainnetForkProvider


__all__ = [
    "GanacheNetworkConfig",
    "GanacheProvider",
    "GanacheProviderError",
    "GanacheSubprocessError",
]
