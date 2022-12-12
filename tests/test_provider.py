import re
import tempfile
from pathlib import Path

import pytest
from ape.exceptions import ContractLogicError, SignatureError
from evm_trace import CallTreeNode, CallType

from ape_ganache.exceptions import GanacheProviderError
from ape_ganache.provider import GANACHE_CHAIN_ID

TEST_WALLET_ADDRESS = "0xD9b7fdb3FC0A0Aa3A507dCf0976bc23D49a9C7A3"


@pytest.fixture(scope="module")
def call_expression():
    return re.compile(r"CALL: 0x([a-f]|[A-F]|\d)*.<0x([a-f]|[A-F]|\d)*> \[\d* gas]")


def test_instantiation(disconnected_provider):
    assert disconnected_provider.name == "ganache"


def test_connect_and_disconnect(disconnected_provider):
    # Use custom port to prevent connecting to a port used in another test.

    disconnected_provider.port = 8555
    disconnected_provider.connect()

    try:
        assert disconnected_provider.is_connected
        assert disconnected_provider.chain_id == GANACHE_CHAIN_ID
    finally:
        disconnected_provider.disconnect()

    assert not disconnected_provider.is_connected
    assert disconnected_provider.process is None


def test_gas_price(connected_provider):
    gas_price = connected_provider.gas_price
    assert gas_price > 1


def test_uri_disconnected(disconnected_provider):
    with pytest.raises(
        GanacheProviderError, match=r"Can't build URI before `connect\(\)` is called\."
    ):
        _ = disconnected_provider.uri


def test_uri(connected_provider):
    expected_uri = f"http://127.0.0.1:{connected_provider.port}"
    assert expected_uri in connected_provider.uri


def test_set_timestamp(connected_provider):
    start_time = connected_provider.get_block("pending").timestamp
    connected_provider.set_timestamp(start_time + 5)  # Increase by 5 seconds

    # Unfortunately, for ganache, you have to mine to see the time difference.
    connected_provider.mine()

    new_time = connected_provider.get_block("pending").timestamp

    # Adding 5 seconds but seconds can be weird so give it a 1 second margin.
    expected = new_time - start_time
    assert 4 <= expected <= 6


def test_mine(connected_provider):
    block_num = connected_provider.get_block("latest").number
    connected_provider.mine(100)
    next_block_num = connected_provider.get_block("latest").number
    assert next_block_num > block_num


def test_revert_failure(connected_provider):
    assert connected_provider.revert(0xFFFF) is False


def test_get_balance(connected_provider, owner):
    assert connected_provider.get_balance(owner.address)


def test_snapshot_and_revert(connected_provider):
    snap = connected_provider.snapshot()

    block_1 = connected_provider.get_block("latest")
    connected_provider.mine()
    block_2 = connected_provider.get_block("latest")
    assert block_2.number > block_1.number
    assert block_1.hash != block_2.hash

    connected_provider.revert(snap)
    block_3 = connected_provider.get_block("latest")
    assert block_1.number == block_3.number
    assert block_1.hash == block_3.hash


def test_get_call_tree(connected_provider, sender, receiver):
    transfer = sender.transfer(receiver, 1)
    call_tree = connected_provider.get_call_tree(transfer.txn_hash)
    assert isinstance(call_tree, CallTreeNode)
    assert call_tree.call_type == CallType.CALL
    assert repr(call_tree) == "CALL: 0xc89D42189f0450C2b2c3c61f58Ec5d628176A1E7 [0 gas]"


def test_request_timeout(connected_provider, config):
    # Test value set in `ape-config.yaml`
    expected = 29
    actual = connected_provider.web3.provider._request_kwargs["timeout"]
    assert actual == expected

    # Test default behavior
    with tempfile.TemporaryDirectory() as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        with config.using_project(temp_dir):
            assert connected_provider.timeout == 30


def test_send_transaction(contract_instance, owner):
    contract_instance.setNumber(10, sender=owner)
    assert contract_instance.myNumber() == 10

    # Have to be in the same test because of X-dist complications
    with pytest.raises(SignatureError):
        contract_instance.setNumber(20)


def test_revert(sender, contract_instance):
    # 'sender' is not the owner so it will revert (with a message)
    with pytest.raises(ContractLogicError, match="!authorized"):
        contract_instance.setNumber(6, sender=sender)


def test_contract_revert_no_message(owner, contract_instance):
    # The Contract raises empty revert when setting number to 5.
    with pytest.raises(ContractLogicError, match="Transaction failed."):
        contract_instance.setNumber(5, sender=owner)


def test_return_value(connected_provider, contract_instance, owner):
    receipt = contract_instance.setAddress(owner.address, sender=owner)
    assert receipt.return_value == 123


def test_get_receipt(connected_provider, contract_instance, owner):
    receipt = contract_instance.setAddress(owner.address, sender=owner)
    actual = connected_provider.get_receipt(receipt.txn_hash)
    assert receipt.txn_hash == actual.txn_hash
    assert actual.receiver == contract_instance.address
    assert actual.sender == receipt.sender
