import random
import shutil
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Any, List, Literal, Optional, Union, cast

from ape.api import (
    PluginConfig,
    ProviderAPI,
    ReceiptAPI,
    SubprocessProvider,
    TestProviderAPI,
    TransactionAPI,
    UpstreamProvider,
    Web3Provider,
)
from ape.exceptions import (
    ContractLogicError,
    OutOfGasError,
    ProviderError,
    SubprocessError,
    TransactionError,
    VirtualMachineError,
)
from ape.logging import logger
from ape.types import SnapshotID
from ape.utils import cached_property, gas_estimation_error_message
from ape_test import Config as TestConfig
from web3 import HTTPProvider, Web3
from web3.gas_strategies.rpc import rpc_gas_price_strategy

from .exceptions import GanacheNotInstalledError, GanacheProviderError

EPHEMERAL_PORTS_START = 49152
EPHEMERAL_PORTS_END = 60999
GANACHE_START_NETWORK_RETRIES = [0.1, 0.2, 0.3, 0.5, 1.0]  # seconds between network retries
GANACHE_START_PROCESS_ATTEMPTS = 3  # number of attempts to start subprocess before giving up
DEFAULT_PORT = 8545
GANACHE_CHAIN_ID = 1337


class GanacheServerConfig(PluginConfig):
    port: Union[int, Literal["auto"]] = "auto"


class GanacheForkConfig(PluginConfig):
    upstream_provider: Optional[str] = None  # Default is to use default upstream provider
    block_number: Optional[int] = None


class GanacheNetworkConfig(PluginConfig):
    # For setting the values in --server.* command arguments.
    # Used whenever ganache is started
    server: GanacheServerConfig = GanacheServerConfig()

    # For setting the values in --fork.* command arguments.
    # Used only in GanacheMainnetForkProvider.
    fork: GanacheForkConfig = GanacheForkConfig()


def _call(*args):
    return Popen([*args], stderr=PIPE, stdout=PIPE, stdin=PIPE)


class GanacheProvider(SubprocessProvider, Web3Provider, TestProviderAPI):
    port: Optional[int] = None
    attempted_ports: List[int] = []

    @cached_property
    def _test_config(self) -> TestConfig:
        return cast(TestConfig, self.config_manager.get_config("test"))

    @property
    def mnemonic(self) -> str:
        return self._test_config.mnemonic

    @property
    def number_of_accounts(self) -> int:
        return self._test_config.number_of_accounts

    @property
    def process_name(self) -> str:
        return "Ganache"

    @cached_property
    def ganache_bin(self) -> str:
        bin = shutil.which("ganache")

        if not bin or _call(bin, "--help") == 0:
            raise GanacheNotInstalledError()

        return bin

    @property
    def project_folder(self) -> Path:
        return self.config_manager.PROJECT_FOLDER

    @property
    def uri(self) -> str:
        if not self.port:
            raise GanacheProviderError("Can't build URI before `connect()` is called.")

        return f"http://127.0.0.1:{self.port}"

    @property
    def priority_fee(self) -> int:
        """
        Priority fee not needed in development network.
        """
        return 0

    @property
    def is_connected(self) -> bool:
        self._set_web3()
        return self._web3 is not None

    def connect(self):
        """
        Start the ganache process and verify it's up and accepting connections.
        """

        # NOTE: Must set port before calling 'super().connect()'.
        if not self.port:
            self.port = self.config.server.port  # type: ignore

        if self.is_connected:
            # Connects to already running process
            self._start()
        else:
            # Only do base-process setup if not connecting to already-running process
            super().connect()

            if self.port:
                self._set_web3()
                if not self._web3:
                    self._start()
                else:
                    # The user configured a port and the ganache process was already running.
                    logger.info(
                        f"Connecting to existing '{self.process_name}' at port '{self.port}'."
                    )
            else:
                for _ in range(self.config.process_attempts):  # type: ignore
                    try:
                        self._start()
                        break
                    except GanacheNotInstalledError:
                        # Is a sub-class of `GanacheSubprocessError` but we to still raise
                        # so we don't keep retrying.
                        raise
                    except SubprocessError as exc:
                        logger.info("Retrying Ganache subprocess startup: %r", exc)
                        self.port = None

    def _set_web3(self):
        if not self.port:
            return

        self._web3 = Web3(HTTPProvider(self.uri))
        if not self._web3.isConnected():
            self._web3 = None
            return

        # Verify is actually a Ganache provider,
        # or else skip it to possibly try another port.
        client_version = self._web3.clientVersion

        if "ganache" in client_version.lower():
            self._web3.eth.set_gas_price_strategy(rpc_gas_price_strategy)
        else:
            raise ProviderError(
                f"Port '{self.port}' already in use by another process that isn't a Ganache node."
            )

    def _start(self):
        use_random_port = self.port == "auto"
        if use_random_port:
            self.port = None

            if DEFAULT_PORT not in self.attempted_ports and not use_random_port:
                self.port = DEFAULT_PORT
            else:
                port = random.randint(EPHEMERAL_PORTS_START, EPHEMERAL_PORTS_END)
                max_attempts = 25
                attempts = 0
                while port in self.attempted_ports:
                    port = random.randint(EPHEMERAL_PORTS_START, EPHEMERAL_PORTS_END)
                    attempts += 1
                    if attempts == max_attempts:
                        ports_str = ", ".join(self.attempted_ports)
                        raise GanacheProviderError(
                            f"Unable to find an available port. Ports tried: {ports_str}"
                        )

                self.port = port

        self.attempted_ports.append(self.port)
        self.start()

    def disconnect(self):
        self._web3 = None
        self.port = None
        super().disconnect()

    def build_command(self) -> List[str]:
        return [
            self.ganache_bin,
            "--server.port",
            str(self.port),
            "--wallet.seed",
            '"' + self.mnemonic + '"',
            "--wallet.totalAccounts",
            str(self.number_of_accounts),
        ]

    def _make_request(self, rpc: str, args: list) -> Any:
        return self._web3.manager.request_blocking(rpc, args)  # type: ignore

    def set_timestamp(self, new_timestamp: int):
        self._make_request("evm_setTime", [new_timestamp])

    def mine(self, num_blocks: int = 1):
        for i in range(num_blocks):
            self._make_request("evm_mine", [])

    def snapshot(self) -> str:
        result = self._make_request("evm_snapshot", [])
        return str(result)

    def revert(self, snapshot_id: SnapshotID):
        if isinstance(snapshot_id, str) and snapshot_id.isnumeric():
            snapshot_id = int(snapshot_id)  # type: ignore

        return self._make_request("evm_revert", [snapshot_id])

    def estimate_gas_cost(self, txn: TransactionAPI, **kwargs) -> int:
        """
        Generates and returns an estimate of how much gas is necessary
        to allow the transaction to complete.
        The transaction will not be added to the blockchain.
        """
        try:
            return super().estimate_gas_cost(txn, **kwargs)
        except ValueError as err:
            tx_error = _get_vm_error(err)

            # If this is the cause of a would-be revert,
            # raise ContractLogicError so that we can confirm tx-reverts.
            if isinstance(tx_error, ContractLogicError):
                raise tx_error from err

            message = gas_estimation_error_message(tx_error)
            raise TransactionError(base_err=tx_error, message=message) from err

    def send_transaction(self, txn: TransactionAPI) -> ReceiptAPI:
        """
        Creates a new message call transaction or a contract creation
        for signed transactions.
        """
        try:
            receipt = super().send_transaction(txn)
        except ValueError as err:
            raise _get_vm_error(err) from err

        receipt.raise_for_status()
        return receipt


class GanacheMainnetForkProvider(GanacheProvider):
    """
    A Ganache provider that uses ``--fork``, like:
    ``ganache --fork.url <upstream-provider-url>``.

    Set the ``upstream_provider`` in the ``ganache.fork`` config
    section of your ``ape-config.yaml` file to specify which provider
    to use as your archive node.
    """

    @property
    def _fork_config(self) -> GanacheForkConfig:
        config = cast(GanacheNetworkConfig, self.config)
        return config.fork

    @cached_property
    def _upstream_provider(self) -> ProviderAPI:
        # NOTE: if 'upstream_provider_name' is 'None', this gets the default mainnet provider.
        if self.network.ecosystem.name != "ethereum":
            raise GanacheProviderError("Fork mode only works for the ethereum ecosystem.")
        mainnet = self.network.ecosystem.mainnet
        upstream_provider_name = self._fork_config.upstream_provider
        upstream_provider = mainnet.get_provider(provider_name=upstream_provider_name)
        return upstream_provider

    def connect(self):
        super().connect()

        # Verify that we're connected to a Ganache node with mainnet-fork mode.
        self._upstream_provider.connect()
        upstream_genesis_block_hash = self._upstream_provider.get_block(0).hash
        self._upstream_provider.disconnect()
        if self.get_block(0).hash != upstream_genesis_block_hash:
            self.disconnect()
            raise GanacheProviderError(
                f"Upstream network is not {self.network.name.replace('-fork', '')}"
            )

    def build_command(self) -> List[str]:
        if not isinstance(self._upstream_provider, UpstreamProvider):
            raise GanacheProviderError(
                f"Provider '{self._upstream_provider.name}' is not an upstream provider."
            )

        fork_url = self._upstream_provider.connection_str
        if not fork_url:
            raise GanacheProviderError("Upstream provider does not have a ``connection_str``.")

        if fork_url.replace("localhost", "127.0.0.1") == self.uri:
            raise GanacheProviderError(
                "Invalid upstream-fork URL. Can't be same as local Ganache node."
            )

        cmd = super().build_command()
        cmd.extend(
            [
                "--fork.url",
                fork_url,
                "--fork.network",
                self._upstream_provider.name,
            ]
        )
        fork_block_number = self._fork_config.block_number
        if fork_block_number is not None:
            cmd.extend(("--fork.blockNumber", str(fork_block_number)))

        return cmd


def _get_vm_error(web3_value_error: ValueError) -> TransactionError:
    if not len(web3_value_error.args):
        return VirtualMachineError(base_err=web3_value_error)

    err_data = web3_value_error.args[0]
    if not isinstance(err_data, dict):
        return VirtualMachineError(base_err=web3_value_error)

    message = str(err_data.get("message"))
    if not message:
        return VirtualMachineError(base_err=web3_value_error)

    # Handle `ContactLogicError` similarly to other providers in `ape`.
    # by stripping off the unnecessary prefix that ganache has on reverts.
    ganache_prefix = (
        "Error: VM Exception while processing transaction: reverted with reason string "
    )
    if message.startswith(ganache_prefix):
        message = message.replace(ganache_prefix, "").strip("'")
        return ContractLogicError(revert_message=message)
    elif "Transaction reverted without a reason string" in message:
        return ContractLogicError()

    elif message == "Transaction ran out of gas":
        return OutOfGasError()

    return VirtualMachineError(message=message)
