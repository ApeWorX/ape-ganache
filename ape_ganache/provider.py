import random
import shutil
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Dict, Iterator, List, Literal, Optional, Union, cast

from ape.api import (
    PluginConfig,
    ProviderAPI,
    SubprocessProvider,
    TestProviderAPI,
    UpstreamProvider,
    Web3Provider,
)
from ape.exceptions import ContractLogicError, ProviderError, SubprocessError, VirtualMachineError
from ape.logging import logger
from ape.types import SnapshotID
from ape.utils import cached_property
from ape_test import Config as TestConfig
from evm_trace import CallTreeNode, CallType, TraceFrame, get_calltree_from_geth_trace
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.exceptions import ExtraDataLengthError
from web3.gas_strategies.rpc import rpc_gas_price_strategy
from web3.middleware import geth_poa_middleware
from web3.middleware.validation import MAX_EXTRADATA_LENGTH

from .exceptions import GanacheNotInstalledError, GanacheProviderError

EPHEMERAL_PORTS_START = 49152
EPHEMERAL_PORTS_END = 60999
DEFAULT_PORT = 8545
GANACHE_CHAIN_ID = 1337


class GanacheServerConfig(PluginConfig):
    port: Union[int, Literal["auto"]] = DEFAULT_PORT


class GanacheForkConfig(PluginConfig):
    upstream_provider: Optional[str] = None  # Default is to use default upstream provider
    block_number: Optional[int] = None


class GanacheNetworkConfig(PluginConfig):
    # For setting the values in --server.* command arguments.
    # Used whenever ganache is started
    server: GanacheServerConfig = GanacheServerConfig()

    # For setting the values in --fork.* command arguments.
    # Used only in GanacheForkProvider.
    fork: Dict[str, Dict[str, GanacheForkConfig]] = {}

    # Retry strategy configs, try increasing these if you're getting GanacheSubprocessError
    request_timeout: int = 30
    fork_request_timeout: int = 300


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

    @property
    def timeout(self) -> int:
        return self.config.request_timeout

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
            self.port = self.provider_settings.get("port", self.config.server.port)

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
                for _ in range(self.config.process_attempts):
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

        self._web3 = Web3(HTTPProvider(self.uri, request_kwargs={"timeout": self.timeout}))

        if not self._web3.is_connected():
            self._web3 = None
            return

        # Verify is actually a Ganache provider,
        # or else skip it to possibly try another port.
        # TODO: Once we are on web3.py 0.6.0b8 or later, can just use snake_case here.
        client_version = getattr(self._web3, "client_version", getattr(self._web3, "clientVersion"))

        if "ganache" in client_version.lower():
            self._web3.eth.set_gas_price_strategy(rpc_gas_price_strategy)
        else:
            raise ProviderError(
                f"Port '{self.port}' already in use by another process that isn't a Ganache server."
            )

        # Handle if using PoA
        try:
            block = self.web3.eth.get_block(0)
        except ExtraDataLengthError:
            began_poa = True
        else:
            began_poa = (
                "proofOfAuthorityData" in block
                or len(block.get("extraData", "")) > MAX_EXTRADATA_LENGTH
            )

        if began_poa:
            self._web3.middleware_onion.inject(geth_poa_middleware, layer=0)

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
            "--wallet.mnemonic",
            self.mnemonic,
            "--wallet.totalAccounts",
            str(self.number_of_accounts),
            "--wallet.hdPath",
            "m/44'/60'/0'",
        ]

    def set_timestamp(self, new_timestamp: int):
        new_timestamp *= 10**3  # Convert to milliseconds
        new_timestamp_hex = HexBytes(new_timestamp).hex()
        self._make_request("evm_setTime", [new_timestamp_hex])

    def mine(self, num_blocks: int = 1):
        for i in range(num_blocks):
            self._make_request("evm_mine", [])

    def snapshot(self) -> str:
        result = self._make_request("evm_snapshot", [])
        return str(result)

    def revert(self, snapshot_id: SnapshotID):
        if isinstance(snapshot_id, str) and snapshot_id.isnumeric():
            snapshot_id = int(snapshot_id)

        return self._make_request("evm_revert", [snapshot_id])

    def get_transaction_trace(self, txn_hash: str) -> Iterator[TraceFrame]:
        result = self._make_request("debug_traceTransaction", [txn_hash])
        frames = result.get("structLogs", [])
        for frame in frames:
            yield TraceFrame(**frame)

    def get_call_tree(self, txn_hash: str) -> CallTreeNode:
        receipt = self.chain_manager.get_receipt(txn_hash)

        # Subtract base gas costs.
        # (21_000 + 4 gas per 0-byte and 16 gas per non-zero byte).
        data_gas = sum([4 if x == 0 else 16 for x in receipt.data])
        method_gas_cost = receipt.gas_used - 21_000 - data_gas

        root_node_kwargs = {
            "gas_cost": method_gas_cost,
            "gas_limit": receipt.gas_limit,
            "address": receipt.receiver,
            "calldata": receipt.data,
            "value": receipt.value,
            "call_type": CallType.CALL,
            "failed": receipt.failed,
        }
        tree = get_calltree_from_geth_trace(receipt.trace, **root_node_kwargs)

        # Strange bug in Ganache where sub-calls REVERT trickles to the top-level
        # CALL when it is not supposed to. Reset `failed`.
        tree.failed = receipt.failed

        return tree

    def get_virtual_machine_error(self, exception: Exception) -> VirtualMachineError:
        if not len(exception.args):
            return VirtualMachineError(base_err=exception)

        err_data = exception.args[0]
        if isinstance(err_data, dict):
            message = str(err_data.get("message"))
        elif isinstance(err_data, str):
            # The message is already extract during gas estimation
            message = str(err_data)
        else:
            return VirtualMachineError(base_err=exception)

        if not message:
            return VirtualMachineError(base_err=exception)

        # Handle `ContactLogicError` similarly to other providers in `ape`.
        # by stripping off the unnecessary prefix that ganache has on reverts.
        ganache_prefix = "VM Exception while processing transaction: "
        prefixes = (f"execution reverted: {ganache_prefix}", ganache_prefix)
        is_revert = False
        for prefix in prefixes:
            if message.startswith(prefix):
                message = message.replace(prefix, "")
                is_revert = True
                break

        if not is_revert:
            return VirtualMachineError(message=message)

        elif message == "revert":
            return ContractLogicError()

        elif message.startswith("revert "):
            message = message.replace("revert ", "")

        return ContractLogicError(revert_message=message)


class GanacheForkProvider(GanacheProvider):
    """
    A Ganache provider that uses ``--fork``, like:
    ``ganache --fork.url <upstream-provider-url>``.

    Set the ``upstream_provider`` in the ``ganache.fork`` config
    section of your ``ape-config.yaml` file to specify which provider
    to use as your archive node.
    """

    @property
    def timeout(self) -> int:
        return self.config.fork_request_timeout

    @property
    def _upstream_network_name(self) -> str:
        return self.network.name.replace("-fork", "")

    @cached_property
    def _fork_config(self) -> GanacheForkConfig:
        config = cast(GanacheNetworkConfig, self.config)

        ecosystem_name = self.network.ecosystem.name
        if ecosystem_name not in config.fork:
            return GanacheForkConfig()  # Just use default

        network_name = self._upstream_network_name
        if network_name not in config.fork[ecosystem_name]:
            return GanacheForkConfig()  # Just use default

        return config.fork[ecosystem_name][network_name]

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

        # Verify that we're connected to a Foundry node with fork mode.
        upstream_provider = self._upstream_provider
        upstream_provider.connect()
        try:
            upstream_genesis_block_hash = upstream_provider.get_block(0).hash
        except ExtraDataLengthError as err:
            if isinstance(upstream_provider, Web3Provider):
                logger.error(
                    f"Upstream provider '{upstream_provider.name}' missing Geth PoA middleware."
                )
                upstream_provider.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
                upstream_genesis_block_hash = upstream_provider.get_block(0).hash
            else:
                raise ProviderError(f"Unable to get genesis block: {err}.") from err

        upstream_provider.disconnect()
        if self.get_block(0).hash != upstream_genesis_block_hash:
            logger.warning(
                "Upstream network has mismatching genesis block. "
                "This could be an issue with ganache."
            )

    def build_command(self) -> List[str]:
        if not isinstance(self._upstream_provider, UpstreamProvider):
            raise GanacheProviderError(
                f"Provider '{self._upstream_provider.name}' is not an upstream provider."
            )

        # Using `getattr` because some IDE type checkers get confused.
        fork_url = getattr(self._upstream_provider, "connection_str")
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
            ]
        )
        fork_block_number = self._fork_config.block_number
        if fork_block_number is not None:
            cmd.extend(("--fork.blockNumber", str(fork_block_number)))

        return cmd
