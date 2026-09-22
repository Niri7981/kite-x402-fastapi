"""The same environment variables and defaults as the official templates."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .kite import FACILITATOR_URL, kite_chain_by_name


def env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, "").strip() or fallback


@dataclass(frozen=True)
class Settings:
    pay_to: str
    upstream_url: str
    kite_network: str = "mainnet"
    price_usd: str = "0.001"
    upstream_auth_header: str = "Authorization"
    upstream_auth_value: str = ""
    service_description: str = "Paid API wrapped for the Kite network"
    port: int = 8080
    facilitator_url: str = FACILITATOR_URL

    def __post_init__(self) -> None:
        if not self.pay_to:
            raise ValueError("PAY_TO is required: the Kite wallet address that receives payments")
        upstream = urlsplit(self.upstream_url)
        if upstream.scheme not in ("http", "https") or not upstream.hostname:
            raise ValueError("UPSTREAM_URL is required, e.g. https://api.example.com")
        chain = kite_chain_by_name(self.kite_network)
        chain.money_parser(self.price, chain.network)

    @property
    def price(self) -> str:
        return self.price_usd if self.price_usd.startswith("$") else f"${self.price_usd}"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            pay_to=env("PAY_TO"),
            upstream_url=env("UPSTREAM_URL"),
            kite_network=env("KITE_NETWORK", "mainnet"),
            price_usd=env("PRICE_USD", "0.001"),
            upstream_auth_header=env("UPSTREAM_AUTH_HEADER", "Authorization"),
            upstream_auth_value=env("UPSTREAM_AUTH_VALUE"),
            service_description=env("SERVICE_DESCRIPTION", "Paid API wrapped for the Kite network"),
            port=int(env("PORT", "8080")),
            facilitator_url=env("FACILITATOR_URL", FACILITATOR_URL),
        )
