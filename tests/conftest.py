from pathlib import Path

import pytest  # type: ignore
from ape import Project, networks
from ape.api.networks import LOCAL_NETWORK_NAME, NetworkAPI
from ape.managers.project import ProjectManager

from ape_ganache import GanacheProvider


def get_project() -> ProjectManager:
    return Project(Path(__file__).parent)


def get_ganache_provider(network_api: NetworkAPI):
    return GanacheProvider(
        name="ganache",
        network=network_api,
        request_header={},
        data_folder=Path("."),
        provider_settings={},
    )


@pytest.fixture(scope="session")
def project():
    return get_project()


@pytest.fixture(scope="session")
def network_api():
    return networks.ecosystems["ethereum"][LOCAL_NETWORK_NAME]


@pytest.fixture(scope="session")
def ganache_disconnected(network_api):
    provider = get_ganache_provider(network_api)
    return provider


@pytest.fixture(scope="session")
def ganache_connected(network_api):
    provider = get_ganache_provider(network_api)
    provider.port = "auto"  # For better multi-processing support
    provider.connect()
    try:
        yield provider
    finally:
        provider.disconnect()
