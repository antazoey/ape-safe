from io import BytesIO
from typing import TYPE_CHECKING

from ape import convert
from ape.types import AddressType, HexBytes
from ape.utils import ManagerAccessMixin, cached_property
from eth_abi.packed import encode_packed

from .exceptions import UnsupportedChainError, ValueRequired
from .packages import MANIFESTS_BY_VERSION, PackageType, get_multisend

if TYPE_CHECKING:
    from ape.api import ReceiptAPI, TransactionAPI
    from ape.contracts.base import ContractInstance, ContractTransactionHandler
    from packaging.version import Version


class MultiSend(ManagerAccessMixin):
    """
    Create a sequence of calls to execute at once using ``eth_sendTransaction``
    via the MultiSend contract.

    Usage example::

        from ape_safe import multisend
        from ape import accounts

        # load the safe
        safe = accounts.load("my-safe")

        txn = multisend.MultiSend()
        txn.add(contract.myMethod, *call_args)
        txn.add(contract.myMethod, *call_args)
        ...  # Add as many calls as desired to execute
        txn.add(contract.myMethod, *call_args)
        # or, using a builder pattern:
        txn = multisend.MultiSend()
            .add(contract.myMethod, *call_args)
            .add(contract.myMethod, *call_args)
            ...  # Add as many calls as desired to execute
            .add(contract.myMethod, *call_args)

        # Stage the transaction to publish on-chain
        # NOTE: if not enough signers are available, publish to Safe API instead
        receipt = txn(sender=safe,gas=0)
    """

    def __init__(self, version: "Version" = max(MANIFESTS_BY_VERSION)) -> None:
        """
        Initialize a new MultiSend session object. By default, there are no calls to make.
        """
        self.calls: list[dict] = []
        self.version = version

    @classmethod
    def inject(cls, version: "Version" = max(MANIFESTS_BY_VERSION)):
        """
        Create the multisend module contract on-chain, so we can use it.
        Must use a provider that supports ``debug_setCode``.

        Usage example::

            from ape_safe.multisend import MultiSend

            @pytest.fixture(scope="session")
            def multisend():
                MultiSend.inject()
                return MultiSend()
        """
        active_provider = cls.network_manager.active_provider
        assert active_provider, "Must be connected to an active network to deploy"
        MultiSend = PackageType.MULTISEND(version)

        active_provider.set_code(
            "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D",
            MultiSend.contract_type.get_runtime_bytecode(),
        )

    @cached_property
    def contract(self) -> "ContractInstance":
        chain_id = self.network_manager.active_provider.chain_id
        try:
            return get_multisend(chain_id, self.version)

        except KeyError:
            try:
                return PackageType.MULTISEND(self.version).at(
                    "0x40A2aCCbd92BCA938b02010E17A5b8929b49130D"
                )

            except Exception as e:
                raise UnsupportedChainError() from e

    @property
    def handler(self) -> "ContractTransactionHandler":
        return self.contract.multiSend

    def add(
        self,
        call,
        *args,
        value=0,
    ) -> "MultiSend":
        """
        Append a call to the MultiSend session object.

        Raises:
            :class:`InvalidOption`: If one of the kwarg modifiers is not able to be used.

        Args:
            call: :class:`ContractMethodHandler` The method to call.
            *args: The arguments to invoke the method with.
            value: int The amount of ether to forward with the call.
        """
        self.calls.append(
            {
                "operation": 0,
                "target": call.contract.address,
                "value": value or 0,
                "callData": call.encode_input(*args),
            }
        )
        return self

    def _validate_calls(self, **txn_kwargs) -> None:
        required_value = sum(call["value"] for call in self.calls)
        if required_value > 0:
            if "value" not in txn_kwargs:
                raise ValueRequired(required_value)

            value = self.conversion_manager.convert(txn_kwargs["value"], int)

            if required_value < value:
                raise ValueRequired(required_value)

        # NOTE: Won't fail if `value` is provided otherwise (won't do anything either)

    @property
    def encoded_calls(self):
        return [
            encode_packed(
                ["uint8", "address", "uint256", "uint256", "bytes"],
                [
                    call["operation"],
                    call["target"],
                    call["value"],
                    len(call["callData"]),
                    call["callData"],
                ],
            )
            for call in self.calls
        ]

    def __call__(self, **txn_kwargs) -> "ReceiptAPI":
        """
        Execute the MultiSend transaction. The transaction will broadcast again every time
        the ``Transaction`` object is called.

        Raises:
            :class:`UnsupportedChain`: If there is not an instance of MultiSend deployed
              on the current chain at the expected address.

        Args:
            **txn_kwargs: the kwargs to pass through to the transaction handler.

        Returns:
            :class:`~ape.api.transactions.ReceiptAPI`
        """
        self._validate_calls(**txn_kwargs)
        if "operation" not in txn_kwargs and not txn_kwargs.get("impersonate", False):
            txn_kwargs["operation"] = 1
        return self.handler(b"".join(self.encoded_calls), **txn_kwargs)

    def as_transaction(self, **txn_kwargs) -> "TransactionAPI":
        """
        Encode the MultiSend transaction as a ``TransactionAPI`` object, but do not execute it.

        Returns:
            :class:`~ape.api.transactions.TransactionAPI`
        """
        self._validate_calls(**txn_kwargs)
        # NOTE: Will fail using `self.handler.as_transaction` because handler
        #       expects to be called only via delegatecall
        if "operation" not in txn_kwargs and not txn_kwargs.get("impersonate", False):
            txn_kwargs["operation"] = 1
        return self.network_manager.ecosystem.create_transaction(
            receiver=self.handler.contract.address,
            data=self.handler.encode_input(b"".join(self.encoded_calls)),
            **txn_kwargs,
        )

    def add_from_calldata(self, calldata: bytes):
        """
        Decode all calls from a multisend calldata and add them to this MultiSend.

        Args:
            calldata: Calldata encoding the MultiSend.multiSend call
        """
        _, args = self.contract.decode_input(calldata)
        buffer = BytesIO(args["transactions"])
        while buffer.tell() < len(args["transactions"]):
            operation = int.from_bytes(buffer.read(1), "big")
            target = convert(buffer.read(20), AddressType)
            value = int.from_bytes(buffer.read(32), "big")
            length = int.from_bytes(buffer.read(32), "big")
            data = HexBytes(buffer.read(length))
            self.calls.append(
                {
                    "operation": operation,
                    "target": target,
                    "value": value,
                    "callData": data,
                }
            )
