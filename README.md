# Kite x402 FastAPI

一个最小的 Python/FastAPI 实现，用于把任意 HTTP API 包装成在 Kite 链上按次付费的 x402 服务。

它移植自 Kite 官方的 [TypeScript/Express 与 Go/Gin 模板](https://github.com/gokite-ai/kite-x402-services/tree/893a27509648b660bbba626b0da59619a94f04ab/templates)，使用官方 Python [`x402` SDK](https://pypi.org/project/x402/2.23.0/)，不定义额外的付款协议。

## 行为

`/healthz` 始终免费。`/v1/*` 下的任意 HTTP 方法都受 x402 保护，并转发到 `UPSTREAM_URL`，同时去除路径中的 `/v1`。

```text
客户端 GET /v1/hello
  -> 402 + PAYMENT-REQUIRED
客户端带 PAYMENT-SIGNATURE 重试
  -> facilitator /verify
  -> 上游 GET /hello
  -> facilitator /settle（仅上游返回 < 400）
  -> 上游响应 + PAYMENT-RESPONSE
```

未提供付款、付款无法解析、付款与挑战不匹配或验证失败时，SDK 返回新的 `402 Payment Required` 和 `PAYMENT-REQUIRED`。上游返回 `>= 400` 或无法连接时不会结算。结算失败时不返回上游响应，而是返回 SDK 的 `402` 结算响应，其中包含 `PAYMENT-RESPONSE`。

代理会移除 `PAYMENT-SIGNATURE` 和 hop-by-hop 请求头。配置了 `UPSTREAM_AUTH_VALUE` 时，认证信息只注入给上游，不会返回给调用方。上游连接错误按官方 Express 模板返回：

```json
{"error":"upstream unreachable","detail":"..."}
```

## Kite 网络与付款挑战

| `KITE_NETWORK` | CAIP-2 网络 | 资产 | 原子单位精度 | EIP-712 域 |
| --- | --- | --- | --- | --- |
| `mainnet`（默认） | `eip155:2366` | USDC.e `0x7aB6f3ed87C42eF0aDb67Ed95090f8bF5240149e` | 6 | `Bridged USDC (Kite AI)` / `2` |
| `testnet` | `eip155:2368` | pieUSD `0x38129cf4CE5E183eFF248F42A7D345Bb1B47621A` | 18 | `pieUSD` / `1` |

默认 facilitator 是 `https://facilitator.pieverse.io/v2`。`/v2` 必须保留，因为 SDK 会继续追加 `/supported`、`/verify` 和 `/settle`。

## PyPI 安装

发行包名为 `kite-x402-fastapi`。项目目前尚未发布到 PyPI；发布后可使用：

```bash
python -m pip install kite-x402-fastapi
```

PyPI 页面：**发布完成后在此填写最终链接**。

安装后配置所需环境变量（见下方“配置”），然后运行 `kite-x402-fastapi`。

## 运行示例

需要 Python 3.10 或更高版本。以下示例使用 `uv`：

```bash
uv sync --extra dev
cp .env.example .env
```

编辑 `.env`，至少填写 `PAY_TO` 和 `UPSTREAM_URL`。先在另一终端启动本地上游：

```bash
uv run uvicorn examples.upstream:app --port 8081
```

将 `.env` 中的 `UPSTREAM_URL` 设为 `http://127.0.0.1:8081`，然后启动包装器：

```bash
set -a; source .env; set +a
uv run kite-x402-fastapi
```

未付款请求会收到挑战：

```bash
curl -i 'http://127.0.0.1:8080/v1/hello?name=Kite'
```

使用 Kite Passport 测试真实付款时，请按[官方仓库的测试流程](https://github.com/gokite-ai/kite-x402-services/tree/893a27509648b660bbba626b0da59619a94f04ab#test-with-a-kite-passport-agent)操作；建议先设 `KITE_NETWORK=testnet`。

## 配置

变量及默认值与官方模板相同：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `PAY_TO` | 是 | — | 接收付款的 Kite 钱包地址 |
| `KITE_NETWORK` | 否 | `mainnet` | `mainnet` 或 `testnet` |
| `UPSTREAM_URL` | 是 | — | 被包装的 HTTP 上游地址 |
| `PRICE_USD` | 否 | `0.001` | 每次调用的美元小数字符串，包装器自动添加 `$` |
| `UPSTREAM_AUTH_HEADER` | 否 | `Authorization` | 注入上游的认证请求头名称 |
| `UPSTREAM_AUTH_VALUE` | 否 | 空 | 注入上游的认证值 |
| `SERVICE_DESCRIPTION` | 否 | `Paid API wrapped for the Kite network` | 付款挑战中的资源描述 |
| `PORT` | 否 | `8080` | 服务端口 |
| `FACILITATOR_URL` | 否 | `https://facilitator.pieverse.io/v2` | x402 facilitator 基地址 |

`PRICE_USD` 必须是正的十进制数。主网最多六位小数；测试网最多十八位小数。

## x402 一致性评估

项目包含一个可复用的确定性评估器，用真实 x402 SDK 和 FastAPI 中间件检查付款挑战、拒绝路径、上游失败处理及结算顺序。facilitator 和上游请求都由脚本化 HTTP mock 响应，因此评估不会连接真实服务或提交真实付款。

安装项目后运行文本报告或 JSON 报告：

```bash
kite-x402-evaluate
kite-x402-evaluate --format json
```

也可以选择 Kite 网络、收款地址和挑战价格：

```bash
kite-x402-evaluate --network testnet --pay-to 0x1111111111111111111111111111111111111111 --price-usd 0.001
```

评估覆盖未付款、验证失败、成功付款、重复付款重放、上游 HTTP 错误、错误网络/资产/EIP-712 代币元数据、结算失败及上游无法连接。命令在全部检查通过时退出码为 `0`，有任何失败时退出码为 `1`。Python 调用方也可使用 `kite_x402_fastapi.evaluation.evaluate_service()`；自定义 `app_factory` 需要支持 `settings`、`facilitator` 和 `upstream_client` 参数。

重放保护在应用进程内记录已成功结算付款的 payload 指纹，并拒绝重复提交。记录仅存在于内存中：进程重启后会清空，多 worker 之间也不会共享，并会随该进程成功结算的不同授权数量增长。因此它适合单进程部署的额外防护，不替代 facilitator 或链上 nonce 的跨进程防重放能力。

## 开发与发布检查

在仓库根目录运行以下命令，可执行完整测试、构建 wheel 和 sdist、检查发行包元数据，并在干净虚拟环境中安装 wheel 后启动 CLI：

```bash
python -m pip install ".[dev]"
python -m pytest -q
python -m build
python -m twine check dist/*
python scripts/smoke_installed.py dist/*.whl
```

GitHub Actions 会在每次 push 和 pull request 时运行相同的测试和发行包检查。版本号在 `pyproject.toml` 中手动维护。
