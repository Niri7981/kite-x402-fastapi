"""Network constants and pricing ported from the official Kite templates."""

import re
from dataclasses import dataclass

from x402.schemas import AssetAmount

FACILITATOR_URL = "https://facilitator.pieverse.io/v2"


@dataclass(frozen=True)
class KiteChain:
    network: str
    rpc_url: str
    asset_address: str
    asset_symbol: str
    asset_decimals: int
    eip712_name: str
    eip712_version: str

    def money_parser(self, amount: str | int | float, network: str) -> AssetAmount | None:
        if network != self.network:
            return None
        text = str(amount).strip().removeprefix("$")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            raise ValueError(f"price must be a positive decimal, got {amount}")
        whole, _, fraction = text.partition(".")
        if len(fraction) > self.asset_decimals:
            raise ValueError(f"price has more than {self.asset_decimals} decimals ({self.asset_symbol})")
        # Decimal-string integer arithmetic, without floating-point rounding.
        units = int(whole + fraction.ljust(self.asset_decimals, "0"))
        if units <= 0:
            raise ValueError("price must be positive and at least one token unit")
        return AssetAmount(
            asset=self.asset_address,
            amount=str(units),
            extra={"name": self.eip712_name, "version": self.eip712_version},
        )


KITE_MAINNET = KiteChain(
    "eip155:2366", "https://rpc.gokite.ai",
    "0x7aB6f3ed87C42eF0aDb67Ed95090f8bF5240149e", "USDC.e", 6,
    "Bridged USDC (Kite AI)", "2",
)
KITE_TESTNET = KiteChain(
    "eip155:2368", "https://rpc-testnet.gokite.ai",
    "0x38129cf4CE5E183eFF248F42A7D345Bb1B47621A", "pieUSD", 18,
    "pieUSD", "1",
)


def kite_chain_by_name(name: str) -> KiteChain:
    if name.strip() in ("", "mainnet"):
        return KITE_MAINNET
    if name.strip() == "testnet":
        return KITE_TESTNET
    raise ValueError(f'unknown KITE_NETWORK "{name}" (want mainnet or testnet)')
