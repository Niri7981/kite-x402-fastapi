"""Exercise the real x402 SDK; mock only facilitator/upstream HTTP traffic."""

import gzip

import pytest

from kite_x402_fastapi.evaluation import PAY_TO, TX, MockNetwork as Network, decode, payment_header, running
from kite_x402_fastapi.kite import KITE_MAINNET, KITE_TESTNET


@pytest.mark.parametrize("chain,amount", [(KITE_MAINNET, "1000"), (KITE_TESTNET, "1000000000000000")])
def test_unpaid_challenge(monkeypatch, chain, amount):
    with running(monkeypatch, Network(chain)) as (client, network):
        response = client.get("/v1/hello?name=Kite")
        assert response.status_code == 402
        assert response.json() == {}
        required = decode(response.headers["payment-required"])
        assert required["x402Version"] == 2
        assert required["accepts"] == [{
            "scheme": "exact", "network": chain.network, "asset": chain.asset_address,
            "amount": amount, "payTo": PAY_TO, "maxTimeoutSeconds": 60,
            "extra": {"name": chain.eip712_name, "version": chain.eip712_version},
        }]
        assert required["resource"]["url"] == "http://testserver/v1/hello?name=Kite"
        assert required["resource"]["mimeType"] == "application/json"
        assert network.events == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
@pytest.mark.parametrize("chain", [KITE_MAINNET, KITE_TESTNET])
def test_paid_verify_upstream_settle(monkeypatch, method, chain):
    with running(monkeypatch, Network(chain), upstream_auth_value="Bearer server-secret") as (client, network):
        headers = payment_header(client)
        headers.update({"Authorization": "Bearer buyer", "X-Custom": "kept", "Content-Type": "application/json"})
        body = b'{"input":42}' if method not in ("GET", "HEAD") else b""
        response = client.request(method, "/v1/hello?a=1&a=2&value=%2F", headers=headers, content=body)
        assert response.status_code == 200
        assert response.content == (b"" if method == "HEAD" else network.upstream_body)
        assert response.headers["x-upstream"] == "yes"
        settled = decode(response.headers["payment-response"])
        assert settled["success"] is True
        assert settled["transaction"] == TX
        assert settled["network"] == chain.network
        assert network.events == ["verify", "upstream", "settle"]
        upstream = network.upstream_requests[0]
        assert str(upstream.url) == "https://upstream.example/hello?a=1&a=2&value=%2F"
        assert upstream.method == method
        assert upstream.content == body
        assert "payment-signature" not in upstream.headers
        assert upstream.headers["authorization"] == "Bearer server-secret"
        assert upstream.headers["x-custom"] == "kept"
        assert upstream.headers["host"] == "upstream.example"
        assert "server-secret" not in str(response.headers)
        assert network.facilitator_requests[-1].content == network.facilitator_requests[-2].content


def test_health_and_unprotected_routes(monkeypatch):
    with running(monkeypatch) as (client, network):
        assert client.get("/healthz").json() == {
            "ok": True, "network": "eip155:2366", "asset": "USDC.e", "price": "$0.001",
        }
        assert client.get("/hello").status_code == 404
        assert client.get("/v10/hello").status_code == 404
        assert network.events == []


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_all_methods_require_payment(monkeypatch, method):
    with running(monkeypatch) as (client, network):
        assert client.request(method, "/v1/hello").status_code == 402
        assert network.events == []


@pytest.mark.parametrize("header", ["not-base64", "e30="])
def test_malformed_payment(monkeypatch, header):
    with running(monkeypatch) as (client, network):
        response = client.get("/v1/hello", headers={"PAYMENT-SIGNATURE": header})
        assert response.status_code == 402
        assert "payment-required" in response.headers
        assert network.events == []


def test_rejected_payment(monkeypatch):
    with running(monkeypatch) as (client, network):
        network.verify_valid = False
        response = client.get("/v1/hello", headers=payment_header(client))
        assert response.status_code == 402
        assert decode(response.headers["payment-required"])["error"] == "invalid_signature"
        assert network.events == ["verify"]


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
def test_upstream_error_does_not_settle(monkeypatch, status):
    with running(monkeypatch) as (client, network):
        network.upstream_status = status
        response = client.get("/v1/hello", headers=payment_header(client))
        assert response.status_code == status
        assert response.content == network.upstream_body
        assert "payment-response" not in response.headers
        assert network.events == ["verify", "upstream"]


def test_upstream_unreachable(monkeypatch):
    with running(monkeypatch) as (client, network):
        network.transport_error_at = "upstream"
        response = client.get("/v1/hello", headers=payment_header(client))
        assert response.status_code == 502
        assert response.json() == {"error": "upstream unreachable", "detail": "connection refused"}
        assert network.events == ["verify", "upstream"]


def test_settlement_failure_hides_upstream_body(monkeypatch):
    with running(monkeypatch) as (client, network):
        network.settle_success = False
        response = client.get("/v1/hello", headers=payment_header(client))
        assert response.status_code == 402
        assert response.json() == {}
        settled = decode(response.headers["payment-response"])
        assert settled["success"] is False
        assert settled["errorReason"] == "insufficient_funds"
        assert network.events == ["verify", "upstream", "settle"]


@pytest.mark.parametrize("operation", ["verify", "settle"])
def test_malformed_facilitator_response_is_a_payment_failure(monkeypatch, operation):
    with running(monkeypatch) as (client, network):
        network.bad_response_at = operation
        response = client.get("/v1/hello", headers=payment_header(client))
        # x402's Python resource server turns an invalid facilitator body into
        # a fresh 402 challenge; it never exposes the malformed body to buyers.
        assert response.status_code == 402
        if operation == "verify":
            assert "invalid JSON" in decode(response.headers["payment-required"])["error"]
        else:
            assert "invalid JSON" in decode(response.headers["payment-response"])["errorReason"]
        assert network.events == (["verify"] if operation == "verify" else ["verify", "upstream", "settle"])


def test_compressed_upstream_and_hop_headers(monkeypatch):
    with running(monkeypatch, upstream_auth_header="X-Api-Key", upstream_auth_value="secret") as (client, network):
        original = network.upstream_body
        network.upstream_body = gzip.compress(original)
        network.upstream_headers.update({"content-encoding": "gzip", "connection": "X-Hop", "x-hop": "strip"})
        headers = payment_header(client)
        headers.update({"Connection": "X-Hop", "X-Hop": "strip", "X-Api-Key": "buyer"})
        response = client.get("/v1/hello", headers=headers)
        assert response.content == original
        assert "content-encoding" not in response.headers
        assert "x-hop" not in response.headers
        assert "x-hop" not in network.upstream_requests[0].headers
        assert network.upstream_requests[0].headers["x-api-key"] == "secret"
