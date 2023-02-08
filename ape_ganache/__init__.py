"""
Ape network provider plugin for Ganache (Ethereum development framework and network
implementation written in Node.js).
"""

from ape import plugins
from ape.api.networks import LOCAL_NETWORK_NAME
from ape_ethereum.ecosystem import NETWORKS

from .exceptions import GanacheProviderError, GanacheSubprocessError
from .provider import GanacheForkProvider, GanacheNetworkConfig, GanacheProvider


@plugins.register(plugins.Config)
def config_class():
    return GanacheNetworkConfig


@plugins.register(plugins.ProviderPlugin)
def providers():
    yield "ethereum", LOCAL_NETWORK_NAME, GanacheProvider
    for network in NETWORKS:
        yield "ethereum", f"{network}-fork", GanacheForkProvider
        
    yield "fantom", LOCAL_NETWORK_NAME, GanacheProvider
    yield "fantom", "opera-fork", GanacheForkProvider
    yield "fantom", "testnet-fork", GanacheForkProvider

    yield "arbitrum", LOCAL_NETWORK_NAME, GanacheProvider
    yield "arbitrum", "mainnet-fork", GanacheForkProvider
    yield "arbitrum", "goerli-fork", GanacheForkProvider

    yield "polygon", LOCAL_NETWORK_NAME, GanacheProvider
    yield "polygon", "mainnet-fork", GanacheForkProvider
    yield "polygon", "mumbai-fork", GanacheForkProvider

    yield "optimism", LOCAL_NETWORK_NAME, GanacheProvider
    yield "optimism", "mainnet-fork", GanacheForkProvider
    yield "optimism", "goerli-fork", GanacheForkProvider


__all__ = [
    "GanacheNetworkConfig",
    "GanacheProvider",
    "GanacheForkProvider",
    "GanacheProviderError",
    "GanacheSubprocessError",
]
