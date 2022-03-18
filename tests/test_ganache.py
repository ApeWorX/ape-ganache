import pytest
from hexbytes import HexBytes

from ape_ganache.exceptions import GanacheProviderError
from ape_ganache.providers import GANACHE_CHAIN_ID, GanacheProvider
from tests.conftest import get_ganache_provider

TEST_WALLET_ADDRESS = "0xD9b7fdb3FC0A0Aa3A507dCf0976bc23D49a9C7A3"


def test_instantiation(ganache_disconnected):
    assert ganache_disconnected.name == "ganache"


def test_connect_and_disconnect(network_api):
    # Use custom port to prevent connecting to a port used in another test.

    ganache = get_ganache_provider(network_api)
    ganache.port = 8555
    ganache.connect()

    try:
        assert ganache.is_connected
        assert ganache.chain_id == GANACHE_CHAIN_ID
    finally:
        ganache.disconnect()

    assert not ganache.is_connected
    assert ganache.process is None


def test_gas_price(ganache_connected):
    gas_price = ganache_connected.gas_price
    assert gas_price > 1


def test_uri_disconnected(ganache_disconnected):
    with pytest.raises(GanacheProviderError) as err:
        _ = ganache_disconnected.uri

    assert "Can't build URI before `connect()` is called." in str(err.value)


def test_uri(ganache_connected):
    expected_uri = f"http://127.0.0.1:{ganache_connected.port}"
    assert expected_uri in ganache_connected.uri


@pytest.mark.parametrize(
    "method,args,expected",
    [
        (GanacheProvider.get_nonce, [TEST_WALLET_ADDRESS], 0),
        (GanacheProvider.get_balance, [TEST_WALLET_ADDRESS], 0),
        (GanacheProvider.get_code, [TEST_WALLET_ADDRESS], HexBytes("")),
    ],
)
def test_rpc_methods(ganache_connected, method, args, expected):
    assert method(ganache_connected, *args) == expected


def test_multiple_ganache_instances(network_api):
    """
    Validate the somewhat tricky internal logic of running multiple Ganache subprocesses
    under a single parent process.
    """
    # instantiate the providers (which will start the subprocesses) and validate the ports
    provider_1 = get_ganache_provider(network_api)
    provider_2 = get_ganache_provider(network_api)
    provider_3 = get_ganache_provider(network_api)
    provider_1.port = 8556
    provider_2.port = 8557
    provider_3.port = 8558
    provider_1.connect()
    provider_2.connect()
    provider_3.connect()

    # The web3 clients must be different in the HH provider instances (compared to the
    # behavior of the EthereumProvider base class, where it's a shared classvar)
    assert provider_1._web3 != provider_2._web3 != provider_3._web3

    assert provider_1.port == 8556
    assert provider_2.port == 8557
    assert provider_3.port == 8558

    provider_1.mine()
    provider_2.mine()
    provider_3.mine()
    hash_1 = provider_1.get_block("latest").hash
    hash_2 = provider_2.get_block("latest").hash
    hash_3 = provider_3.get_block("latest").hash
    assert hash_1 != hash_2 != hash_3


@pytest.mark.xfail(reason="Ganache doesn't support evm_setBlockGasLimit yet.")
def test_set_block_gas_limit(ganache_connected):
    gas_limit = ganache_connected.get_block("latest").gas_data.gas_limit
    assert ganache_connected.set_block_gas_limit(gas_limit) is True


@pytest.mark.xfail(reason="https://github.com/trufflesuite/ganache/issues/772")
def test_set_timestamp(ganache_connected):
    start_time = ganache_connected.get_block("pending").timestamp
    ganache_connected.set_timestamp(start_time + 5)  # Increase by 5 seconds
    new_time = ganache_connected.get_block("pending").timestamp

    # Adding 5 seconds but seconds can be weird so give it a 1 second margin.
    assert 4 <= new_time - start_time <= 6


def test_mine(ganache_connected):
    block_num = ganache_connected.get_block("latest").number
    ganache_connected.mine()
    next_block_num = ganache_connected.get_block("latest").number
    assert next_block_num > block_num


def test_revert_failure(ganache_connected):
    assert ganache_connected.revert(0xFFFF) is False


def test_snapshot_and_revert(ganache_connected):
    snap = ganache_connected.snapshot()

    block_1 = ganache_connected.get_block("latest")
    ganache_connected.mine()
    block_2 = ganache_connected.get_block("latest")
    assert block_2.number > block_1.number
    assert block_1.hash != block_2.hash

    ganache_connected.revert(snap)
    block_3 = ganache_connected.get_block("latest")
    assert block_1.number == block_3.number
    assert block_1.hash == block_3.hash


@pytest.mark.xfail(reason="Ganache doesn't support *_impersonateAccount yet.")
def test_unlock_account(ganache_connected):
    assert ganache_connected.unlock_account(TEST_WALLET_ADDRESS) is True
