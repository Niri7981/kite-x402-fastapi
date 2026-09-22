import pytest

from kite_x402_fastapi import Settings
from kite_x402_fastapi.kite import FACILITATOR_URL, KITE_MAINNET, KITE_TESTNET, kite_chain_by_name


@pytest.mark.parametrize("amount,units", [("0.001", "1000"), ("0.000001", "1"), ("1.234567", "1234567"), ("$1", "1000000")])
def test_exact_mainnet_amounts(amount, units):
    assert KITE_MAINNET.money_parser(amount, KITE_MAINNET.network).amount == units


def test_testnet_precision_and_other_network():
    assert KITE_TESTNET.money_parser("0.000000000000000001", KITE_TESTNET.network).amount == "1"
    assert KITE_MAINNET.money_parser("1", KITE_TESTNET.network) is None


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "inf", "1e-3", "abc", "0.0000001"])
def test_invalid_prices(price):
    with pytest.raises(ValueError):
        Settings(pay_to="0x123", upstream_url="https://api.example", price_usd=price)


@pytest.mark.parametrize("url", ["", "invalid://", "ftp://example.com", "https://"])
def test_invalid_upstream(url):
    with pytest.raises(ValueError, match="UPSTREAM_URL"):
        Settings(pay_to="0x123", upstream_url=url)


def test_required_wallet_and_unknown_network():
    with pytest.raises(ValueError, match="PAY_TO"):
        Settings(pay_to="", upstream_url="https://api.example")
    with pytest.raises(ValueError, match="KITE_NETWORK"):
        kite_chain_by_name("base")


def test_official_environment_variables(monkeypatch):
    values = {
        "PAY_TO": " 0x123 ", "UPSTREAM_URL": " https://api.example ",
        "KITE_NETWORK": " testnet ", "PRICE_USD": " $0.002 ",
        "UPSTREAM_AUTH_HEADER": " X-Api-Key ", "UPSTREAM_AUTH_VALUE": " secret ",
        "SERVICE_DESCRIPTION": " Example ", "PORT": " 9090 ",
        "FACILITATOR_URL": " https://facilitator.example/v2 ",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings == Settings(
        pay_to="0x123", upstream_url="https://api.example", kite_network="testnet",
        price_usd="$0.002", upstream_auth_header="X-Api-Key", upstream_auth_value="secret",
        service_description="Example", port=9090, facilitator_url="https://facilitator.example/v2",
    )
    for key in values:
        monkeypatch.setenv(key, " ")
    monkeypatch.setenv("PAY_TO", "0x123")
    monkeypatch.setenv("UPSTREAM_URL", "https://api.example")
    defaults = Settings.from_env()
    assert defaults.price == "$0.001"
    assert defaults.port == 8080
    assert defaults.facilitator_url == FACILITATOR_URL
    assert defaults.kite_network == "mainnet"
