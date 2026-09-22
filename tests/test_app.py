"""Exercise the real x402 SDK; mock only facilitator/upstream HTTP traffic."""

import asyncio
import base64
import gzip
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from x402.http import FacilitatorConfig, HTTPFacilitatorClient

from kite_x402_fastapi import Settings, create_app
from kite_x402_fastapi.kite import KITE_MAINNET, KITE_TESTNET

PAY_TO = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
TX = "0x" + "ab" * 32


def decode(value):
    return json.loads(base64.b64decode(value))


class Network:
    def __init__(self, chain=KITE_MAINNET):
        self.chain = chain
        self.events = []
        self.facilitator_requests = []
        self.upstream_requests = []
        self.verify_valid = True
        self.settle_success = True
        self.bad_response_at = None
        self.transport_error_at = None
        self.upstream_status = 200
        self.upstream_body = b'{"hello":"Kite"}'
        self.upstream_headers = {"content-type": "application/json", "x-upstream": "yes"}

    def facilitator(self, request):
        operation = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "facilitator.example"
        assert request.url.path == f"/v2/{operation}"
        self.events.append(operation)
        self.facilitator_requests.append(request)
        if operation == self.transport_error_at:
            raise httpx.ConnectError("connection refused", request=request)
        if operation == self.bad_response_at:
            return httpx.Response(200, text="not JSON")
        if operation == "supported":
            assert request.method == "GET"
            return httpx.Response(200, json={
                "kinds": [{"x402Version": 2, "scheme": "exact", "network": self.chain.network}],
                "extensions": [], "signers": {},
            })
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["x402Version"] == 2
        assert body["paymentRequirements"] == body["paymentPayload"]["accepted"]
        if operation == "verify":
            return httpx.Response(200, json={
                "isValid": self.verify_valid, "payer": PAYER,
                **({} if self.verify_valid else {"invalidReason": "invalid_signature"}),
            })
        assert operation == "settle"
        assert self.events[-2] == "upstream", "Settlement must follow upstream success"
        return httpx.Response(200, json={
            "success": self.settle_success, "network": self.chain.network,
            "transaction": TX if self.settle_success else "", "payer": PAYER,
            **({} if self.settle_success else {"errorReason": "insufficient_funds"}),
        })

    def upstream(self, request):
        assert self.events[-1] in ("verify", "upstream")
        self.events.append("upstream")
        self.upstream_requests.append(request)
        if self.transport_error_at == "upstream":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(self.upstream_status, content=self.upstream_body, headers=self.upstream_headers)


@contextmanager
def running(monkeypatch, network=None, **overrides):
    network = network or Network()
    facilitator_transport = httpx.MockTransport(network.facilitator)
    # /supported is synchronous in the official SDK; verify and settle are async.
    monkeypatch.setattr(
        HTTPFacilitatorClient, "_get_sync_client",
        lambda self: httpx.Client(transport=facilitator_transport),
    )
    facilitator_http = httpx.AsyncClient(transport=facilitator_transport)
    upstream_http = httpx.AsyncClient(transport=httpx.MockTransport(network.upstream))
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(
        url="https://facilitator.example/v2", http_client=facilitator_http,
    ))
    settings = Settings(
        pay_to=PAY_TO, upstream_url="https://upstream.example/base",
        kite_network="testnet" if network.chain == KITE_TESTNET else "mainnet",
        **overrides,
    )
    try:
        with TestClient(create_app(settings, facilitator=facilitator, upstream_client=upstream_http)) as client:
            assert network.events == ["supported"]
            network.events.clear()
            yield client, network
    finally:
        asyncio.run(facilitator_http.aclose())
        asyncio.run(upstream_http.aclose())


def payment_header(client, path="/v1/hello"):
    challenge = decode(client.get(path).headers["payment-required"])
    # Deliberately synthetic signature: validation belongs to the mocked facilitator.
    payload = {
        "x402Version": 2, "accepted": challenge["accepts"][0],
        "resource": challenge["resource"],
        "payload": {
            "signature": "0x" + "00" * 65,
            "authorization": {
                "from": PAYER, "to": PAY_TO, "value": challenge["accepts"][0]["amount"],
                "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "01" * 32,
            },
        },
    }
    return {"PAYMENT-SIGNATURE": base64.b64encode(json.dumps(payload).encode()).decode()}


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
