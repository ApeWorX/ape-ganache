import tempfile
from pathlib import Path

import pytest
from ape.api.accounts import ImpersonatedAccount
from ape.api.networks import LOCAL_NETWORK_NAME
from ape.contracts import ContractInstance
from ape.exceptions import ContractLogicError
from ape_ethereum.ecosystem import NETWORKS

TESTS_DIRECTORY = Path(__file__).parent
TEST_ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"


@pytest.fixture
def mainnet_fork_contract_instance(owner, contract_container, mainnet_fork_provider):
    return owner.deploy(contract_container)


@pytest.mark.fork
def test_multiple_providers(
    name, networks, connected_provider, mainnet_fork_port, goerli_fork_port
):
    assert networks.active_provider.name == name
    assert networks.active_provider.network.name == LOCAL_NETWORK_NAME
    assert networks.active_provider.port == 8545

    with networks.ethereum.mainnet_fork.use_provider(
        name, provider_settings={"port": mainnet_fork_port}
    ):
        assert networks.active_provider.name == name
        assert networks.active_provider.network.name == "mainnet-fork"
        assert networks.active_provider.port == mainnet_fork_port

        with networks.ethereum.goerli_fork.use_provider(
            name, provider_settings={"port": goerli_fork_port}
        ):
            assert networks.active_provider.name == name
            assert networks.active_provider.network.name == "goerli-fork"
            assert networks.active_provider.port == goerli_fork_port

        assert networks.active_provider.name == name
        assert networks.active_provider.network.name == "mainnet-fork"
        assert networks.active_provider.port == mainnet_fork_port

    assert networks.active_provider.name == name
    assert networks.active_provider.network.name == LOCAL_NETWORK_NAME
    assert networks.active_provider.port == 8545


@pytest.mark.parametrize("network", [k for k in NETWORKS.keys()])
def test_fork_config(name, config, network):
    plugin_config = config.get_config(name)
    network_config = plugin_config["fork"].get("ethereum", {}).get(network, {})
    message = f"Config not registered for network '{network}'."
    assert network_config.get("upstream_provider") == "alchemy", message


@pytest.mark.fork
def test_request_timeout(networks, config, mainnet_fork_provider):
    actual = mainnet_fork_provider.web3.provider._request_kwargs["timeout"]
    expected = 360  # Value set in `ape-config.yaml`
    assert actual == expected

    # Test default behavior
    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        with config.using_project(temp_dir):
            assert networks.active_provider.timeout == 300


@pytest.mark.fork
def test_transaction(owner, mainnet_fork_contract_instance):
    receipt = mainnet_fork_contract_instance.setNumber(6, sender=owner)
    assert receipt.sender == owner

    value = mainnet_fork_contract_instance.myNumber()
    assert value == 6


@pytest.mark.fork
def test_revert(sender, mainnet_fork_contract_instance):
    # 'sender' is not the owner so it will revert (with a message)
    with pytest.raises(ContractLogicError, match="!authorized"):
        mainnet_fork_contract_instance.setNumber(6, sender=sender)


@pytest.mark.fork
def test_contract_revert_no_message(owner, mainnet_fork_contract_instance, mainnet_fork_provider):
    # The Contract raises empty revert when setting number to 5.
    with pytest.raises(ContractLogicError, match="Transaction failed."):
        mainnet_fork_contract_instance.setNumber(5, sender=owner)


@pytest.mark.fork
def test_get_receipt(mainnet_fork_provider, mainnet_fork_contract_instance, owner):
    receipt = mainnet_fork_contract_instance.setAddress(owner.address, sender=owner)
    actual = mainnet_fork_provider.get_receipt(receipt.txn_hash)
    assert receipt.txn_hash == actual.txn_hash
    assert actual.receiver == mainnet_fork_contract_instance.address
    assert actual.sender == receipt.sender


@pytest.mark.fork
def test_unlock_account_with_multiple_providers(
    networks, connected_provider, mainnet_fork_port, goerli_fork_port
):
    with networks.ethereum.mainnet_fork.use_provider(
        "ganache", provider_settings={"port": mainnet_fork_port}
    ):
        imp_acc = connected_provider.account_manager[TEST_ADDRESS]
        assert isinstance(imp_acc, ImpersonatedAccount)

        with networks.ethereum.goerli_fork.use_provider(
            "ganache", provider_settings={"port": goerli_fork_port}
        ):
            imp_acc = connected_provider.account_manager[TEST_ADDRESS]
            assert isinstance(imp_acc, ImpersonatedAccount)

    imp_acc = connected_provider.account_manager[TEST_ADDRESS]
    assert isinstance(imp_acc, ImpersonatedAccount)


@pytest.mark.fork
def test_connect_to_polygon(networks, owner, contract_container):
    """
    Ensures we don't get PoA middleware issue.
    """
    with networks.polygon.mumbai_fork.use_provider("ganache"):
        contract = owner.deploy(contract_container)
        assert isinstance(contract, ContractInstance)  # Didn't fail
