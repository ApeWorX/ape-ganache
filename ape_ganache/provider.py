import random
import shutil
from enum import Enum
from itertools import tee
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Dict, Iterator, List, Literal, Optional, Union, cast

from ape._pydantic_compat import root_validator
from ape.api import (
    ForkedNetworkAPI,
    ImpersonatedAccount,
    PluginConfig,
    SubprocessProvider,
    TestProviderAPI,
    Web3Provider,
)
from ape.exceptions import (
    ContractLogicError,
    ConversionError,
    ProviderError,
    SubprocessError,
    VirtualMachineError,
)
from ape.logging import logger
from ape.types import AddressType, CallTreeNode, SnapshotID, TraceFrame
from ape.utils import cached_property
from ape_test import Config as TestConfig
from eth_utils import to_checksum_address, to_hex
from evm_trace import CallType
from evm_trace import TraceFrame as EvmTraceFrame
from evm_trace import get_calltree_from_geth_trace
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


class Hardforks(Enum):
    CONSTANTINOPLE = "constantinople"
    BYZANTIUM = "byzantium"
    PETERSBURG = "petersburg"
    ISTANBUL = "istanbul"
    MUIR_GLACIER = "muirGlacier"
    BERLIN = "berlin"
    LONDON = "london"
    ARROW_GLACIER = "arrowGlacier"
    GRAY_GLACIER = "grayGlacier"


class GanacheServerConfig(PluginConfig):
    port: Union[int, Literal["auto"]] = DEFAULT_PORT


class GanacheWalletConfig(PluginConfig):
    unlocked_accounts: List[str] = []


class GanacheForkConfig(PluginConfig):
    upstream_provider: Optional[str] = None  # Default is to use default upstream provider
    block_number: Optional[int] = None


class GanacheMinerConfig(PluginConfig):
    gas_price: int = 2_000_000_000


class GanacheChainConfig(PluginConfig):
    hardfork: Hardforks = Hardforks.LONDON


class GanacheNetworkConfig(PluginConfig):
    # For setting the values in --server.* command arguments.
    # Used whenever ganache is started
    server: GanacheServerConfig = GanacheServerConfig()

    # For setting the values in --fork.* command arguments.
    # Used only in GanacheForkProvider.
    fork: Dict[str, Dict[str, GanacheForkConfig]] = {}

    wallet: GanacheWalletConfig = GanacheWalletConfig()
    # wallet allows setting values in --wallet.* command arguments
    # Use the ``test`` config to set the mnemonic, HD Path, and number of accounts.

    # Retry strategy configs, try increasing these if you're getting GanacheSubprocessError
    request_timeout: int = 30
    fork_request_timeout: int = 300
    miner: GanacheMinerConfig = GanacheMinerConfig()
    chain: GanacheChainConfig = GanacheChainConfig()


def _call(*args):
    return Popen([*args], stderr=PIPE, stdout=PIPE, stdin=PIPE)


class GanacheProvider(SubprocessProvider, Web3Provider, TestProviderAPI):
    port: Optional[int] = None
    attempted_ports: List[int] = []

    @cached_property
    def _test_config(self) -> TestConfig:
        return cast(TestConfig, self.config_manager.get_config("test"))

    @property
    def connection_id(self) -> Optional[str]:
        return f"{self.network_choice}:{self.port}"

    @cached_property
    def unlocked_accounts(self) -> List[ImpersonatedAccount]:
        addresses: List[AddressType] = []
        for address in self.settings.wallet.unlocked_accounts:
            if isinstance(address, str) and address.isnumeric():
                # User didn't put quotes around addresses in config file
                address_str = to_hex(int(address)).replace("0x", "")
                address_str = f"0x{'0' * (40 - len(address_str))}{address_str}"
                address = to_checksum_address(address_str)
                addresses.append(address)
            else:
                try:
                    address = self.conversion_manager.convert(address, AddressType)
                except ConversionError as err:
                    logger.error(str(err))
                    continue

                addresses.append(address)

        # Include RPC unlocked accounts
        addresses.extend(self.account_manager.test_accounts._impersonated_accounts)

        return [ImpersonatedAccount(raw_address=x) for x in addresses]

    @property
    def mnemonic(self) -> str:
        return self._test_config.mnemonic

    @property
    def number_of_accounts(self) -> int:
        return self._test_config.number_of_accounts

    @property
    def hd_path(self) -> str:
        return self._test_config.hd_path.replace("/{}", "")

    @property
    def process_name(self) -> str:
        return "Ganache"

    @property
    def timeout(self) -> int:
        return self.settings.request_timeout

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
            if "port" in self.provider_settings:
                self.port = self.provider_settings.pop("port")
            else:
                self.port = (
                    self.provider_settings.get("server", {}).get("port")
                    or self.settings.server.port
                )

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
                for _ in range(self.settings.process_attempts):
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
        client_version = self._web3.client_version.lower()
        if "ganache" in client_version:
            self._web3.eth.set_gas_price_strategy(rpc_gas_price_strategy)
        else:
            raise ProviderError(
                f"Port '{self.port}' already in use by another process that isn't a Ganache server."
            )

        def check_poa(block_id) -> bool:
            try:
                block = self.web3.eth.get_block(block_id)
            except ExtraDataLengthError:
                return True
            else:
                return (
                    "proofOfAuthorityData" in block
                    or len(block.get("extraData", "")) > MAX_EXTRADATA_LENGTH
                )

        # Handle if using PoA
        if any(map(check_poa, (0, "latest"))):
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
        cmd = [
            self.ganache_bin,
            "--server.port",
            str(self.port),
            "--wallet.mnemonic",
            self.mnemonic,
            "--wallet.totalAccounts",
            str(self.number_of_accounts),
            "--wallet.hdPath",
            str(self.hd_path),
            "--chain.hardfork",
            self.settings.chain.hardfork.value,
            "--miner.defaultGasPrice",
            str(self.settings.miner.gas_price),
            "--chain.vmErrorsOnRPCResponse",
            "true",
        ]
        for account in self.unlocked_accounts:
            cmd.extend(("--wallet.unlockedAccounts", account.address))
        return cmd

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
        for trace in self._get_transaction_trace(txn_hash):
            yield self._create_trace_frame(trace)

    def _get_transaction_trace(self, txn_hash: str) -> Iterator[EvmTraceFrame]:
        result = self._make_request("debug_traceTransaction", [txn_hash])
        frames = result.get("structLogs", [])
        for frame in frames:
            yield EvmTraceFrame(**frame)

    def get_call_tree(self, txn_hash: str) -> CallTreeNode:
        receipt = self.chain_manager.get_receipt(txn_hash)

        # Subtract base gas costs.
        # (21_000 + 4 gas per 0-byte and 16 gas per non-zero byte).
        data_gas = sum([4 if x == 0 else 16 for x in receipt.data])
        method_gas_cost = receipt.gas_used - 21_000 - data_gas

        if receipt.contract_address:
            # Deploy txn
            address = receipt.contract_address
            call_type = CallType.CREATE
        else:
            # Invoke txn
            address = receipt.receiver
            call_type = CallType.CALL

        evm_call = get_calltree_from_geth_trace(
            self._get_transaction_trace(txn_hash),
            gas_cost=method_gas_cost,
            gas_limit=receipt.gas_limit,
            address=address,
            calldata=receipt.data,
            value=receipt.value,
            call_type=call_type,
            failed=receipt.failed,
        )
        # Strange bug in Ganache where sub-calls REVERT trickles to the top-level
        # CALL when it is not supposed to. Reset `failed`.
        evm_call.failed = receipt.failed

        return self._create_call_tree_node(evm_call, txn_hash=txn_hash)

    def get_virtual_machine_error(self, exception: Exception, **kwargs) -> VirtualMachineError:
        if not len(exception.args):
            return VirtualMachineError(base_err=exception, **kwargs)

        ganache_prefix = "VM Exception while processing transaction: "
        err_data = exception.args[0]
        if isinstance(err_data, dict):
            message = str(err_data.get("message"))

            if err_data.get("data", {}).get("hash") and message == f"{ganache_prefix}revert":
                txn_hash = err_data.get("data", {}).get("hash")
                data = {}

                if "trace" in kwargs:
                    kwargs["trace"], new_trace = tee(kwargs["trace"])
                    data = list(new_trace)[-1].raw

                else:
                    try:
                        data = list(self.get_transaction_trace(txn_hash))[-1].raw
                    except Exception:
                        pass

                if data.get("op") == "REVERT":
                    err_selector_and_inputs = "".join([x[2:] for x in data["memory"][4:]])
                    if err_selector_and_inputs:
                        message = f"{ganache_prefix}0x{err_selector_and_inputs}"

        elif isinstance(err_data, str):
            # The message is already extract during gas estimation
            message = str(err_data)
        else:
            return VirtualMachineError(base_err=exception, **kwargs)

        if not message:
            return VirtualMachineError(base_err=exception, **kwargs)

        # Handle `ContactLogicError` similarly to other providers in `ape`.
        # by stripping off the unnecessary prefix that ganache has on reverts.
        prefixes = (f"execution reverted: {ganache_prefix}", ganache_prefix)
        is_revert = False
        for prefix in prefixes:
            if message.startswith(prefix):
                message = message.replace(prefix, "")
                is_revert = True
                break

        if not is_revert:
            return VirtualMachineError(message, **kwargs)

        if message == "revert":
            err = ContractLogicError(**kwargs)
            return self.compiler_manager.enrich_error(err)

        elif message.startswith("revert "):
            message = message.replace("revert ", "")

        err = ContractLogicError(revert_message=message, **kwargs)
        return self.compiler_manager.enrich_error(err)

    def unlock_account(self, address: AddressType) -> bool:
        self._make_request("evm_addAccount", [address, ""])
        return self._make_request("personal_unlockAccount", [address, "", 9999999999])


class GanacheForkProvider(GanacheProvider):
    """
    A Ganache provider that uses ``--fork``, like:
    ``ganache --fork.url <upstream-provider-url>``.

    Set the ``upstream_provider`` in the ``ganache.fork`` config
    section of your ``ape-config.yaml` file to specify which provider
    to use as your archive node.
    """

    @root_validator()
    def set_upstream_provider(cls, value):
        network = value["network"]
        adhoc_settings = value.get("provider_settings", {}).get("fork", {})
        ecosystem_name = network.ecosystem.name
        plugin_config = cls.config_manager.get_config(value["name"])
        config_settings = plugin_config.get("fork", {})

        def _get_upstream(data: Dict) -> Optional[str]:
            return (
                data.get(ecosystem_name, {})
                .get(network.name.replace("-fork", ""), {})
                .get("upstream_provider")
            )

        # If upstream provider set anywhere in provider settings, ignore.
        if name := (_get_upstream(adhoc_settings) or _get_upstream(config_settings)):
            getattr(network.ecosystem.config, network.name).upstream_provider = name

        return value

    @property
    def timeout(self) -> int:
        return self.settings.fork_request_timeout

    @property
    def _upstream_network_name(self) -> str:
        return self.network.name.replace("-fork", "")

    @cached_property
    def _fork_config(self) -> GanacheForkConfig:
        settings = cast(GanacheNetworkConfig, self.settings)

        ecosystem_name = self.network.ecosystem.name
        if ecosystem_name not in settings.fork:
            return GanacheForkConfig()  # Just use default

        network_name = self._upstream_network_name
        if network_name not in settings.fork[ecosystem_name]:
            return GanacheForkConfig()  # Just use default

        return settings.fork[ecosystem_name][network_name]

    @property
    def forked_network(self) -> ForkedNetworkAPI:
        return cast(ForkedNetworkAPI, self.network)

    def connect(self):
        super().connect()

        # If using the provider config for upstream_provider,
        # set the network one in this session, so other features work in core.
        with self.forked_network.use_upstream_provider() as upstream_provider:
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
                    raise GanacheProviderError(f"Unable to get genesis block: {err}.") from err

        if self.get_block(0).hash != upstream_genesis_block_hash:
            logger.warning(
                "Upstream network has mismatching genesis block. "
                "This could be an issue with foundry."
            )

    def build_command(self) -> List[str]:
        fork_url = self.forked_network.upstream_provider.connection_str
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
