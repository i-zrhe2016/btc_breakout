# BTC Breakout

一个面向 Binance USDⓈ-M Futures 的截图画线策略工作台。粘贴入场线截图，并可选择是否添加止损线截图，AI 识别水平线或趋势线；人工确认参数后，服务端持续监控合约价格并执行 LONG/SHORT 入场及可选止损。

默认只运行模拟模式。实盘必须显式设置 `ENABLE_LIVE_FUTURES=true`，并通过二次确认。

## 主要能力

- 入场线必选、止损线可选；两者都支持粘贴、拖放或选择截图
- 水平线和两点趋势线识别，识别结果可人工修改
- 同一张 K 线图预览两条线及其当前投影价
- LONG：价格向上触发入场，向下触发止损
- SHORT：价格向下触发入场，向上触发止损
- 价格在启用时已越过入场线，会立即触发
- Binance 合约逐笔成交 WebSocket，异常时自动降级 REST 行情
- 模拟和实盘、固定 USDT 名义仓位、1–125 倍杠杆、全仓模式
- SQLite 保存策略、事件和成交状态；重启后恢复监控
- 实盘重启恢复前核对交易所持仓，平仓使用 `reduceOnly` 且不超过实际仓位
- 每个交易对只允许一个活动实盘策略

旧版现货趋势线监控仍保留在 `/manual`、`/ai` 和 `/signal/watch`。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

或使用 Docker：

```bash
docker compose up -d --build
docker compose logs -f api
```

入口：

- 新策略工作台：`http://127.0.0.1:8000/`
- 旧版手动画线：`http://127.0.0.1:8000/manual`
- 旧版单截图识别：`http://127.0.0.1:8000/ai`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`
- 公网域名：`https://line.zrhe2016.cc/`（Cloudflare DNS 需指向部署服务器）

## 使用流程

1. 设置交易对、周期、LONG/SHORT、名义仓位和杠杆。
2. 在“入场线”卡片粘贴图表截图，选择自动/水平线/趋势线，然后识别。
3. 按需开启“使用止损线”，再在止损线卡片重复操作；关闭时入场后不会自动止损。
4. 检查回画位置和精确价格；必要时直接编辑水平价格或两个趋势线锚点。
5. 先用模拟模式启用策略。确认逻辑无误后再切换实盘并完成风险确认。
6. 状态区会展示行情连接、当前价、两条线投影价、成交和事件记录。

粘贴快捷键是 `Ctrl+V`（macOS 为 `⌘V`）。截图支持 PNG、JPEG、WebP，单张最大 10 MB。识别时请确保价格轴和时间轴清晰，并在“目标线提示”里写明颜色或位置。

启用止损线时，它必须位于当前价格的风险侧：LONG 止损低于当前价，SHORT 止损高于当前价。未启用止损线的策略入场后会保持仓位，直到用户手动取消并选择平仓；当前版本不设置止盈。

## 环境变量

### Codex 截图识别

- `CODEX_AUTH_FILE`：宿主机 Codex CLI 登录文件，Compose 默认 `/root/.codex/auth.json`
- `CODEX_MODEL`：可选；留空时使用 Codex CLI 默认模型
- `CODEX_TIMEOUT_SECONDS`：单次截图识别超时，默认 180 秒，可用范围 30–600 秒

截图识别直接执行容器内的 `codex exec`，使用 `--ephemeral`、只读沙箱和 JSON Schema 输出，不需要 `OPENAI_API_KEY`。部署前先确认宿主机已经登录：

```bash
codex login status
docker compose up -d --build
```

Compose 只把 `auth.json` 挂载到容器的 `/codex-home/auth.json`，不会挂载完整的 `~/.codex`。该文件包含登录令牌，应按密码保护、禁止提交到 Git，并保持可写以便 Codex 刷新令牌。非 root 用户或自定义 Codex 目录可设置 `CODEX_AUTH_FILE=/path/to/auth.json`。

### Binance Futures

- `BINANCE_FUTURES_BASE_URL`：默认 `https://fapi.binance.com`
- `BINANCE_FUTURES_WS_URL`：默认 `wss://fstream.binance.com/ws`
- `BINANCE_API_KEY`、`BINANCE_API_SECRET`：仅实盘需要
- `ENABLE_LIVE_FUTURES`：只有 `true/1/yes/on` 才允许实盘
- `STRATEGY_DB_PATH`：默认项目目录下 `strategy_state.db`；Compose 使用 `/data/strategy_state.db`

### 通用和旧版

- `BINANCE_BASE_URL`：旧版现货接口地址
- `BARK_NOTIFY_URL`：旧版突破通知地址
- `CORS_ALLOW_ORIGINS`：默认 `*`
- `LOG_LEVEL`：默认 `INFO`
- `TELEGRAM_BOT_TOKEN`、`TELEGRAM_ALLOWED_CHAT_IDS`：可选旧版 Telegram 机器人

实盘示例：

```bash
BINANCE_API_KEY=your_key \
BINANCE_API_SECRET=your_secret \
ENABLE_LIVE_FUTURES=true \
docker compose up -d --build
```

建议 API Key 只授予合约交易权限，不要授予提现权限，并使用 IP 白名单。

## 新接口

### `POST /ai/line-recognize`

识别一张入场或止损截图。主要字段：

```json
{
  "image_data_url": "data:image/png;base64,<...>",
  "role": "entry",
  "expected_line_type": "auto",
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "chart_timezone": "UTC",
  "target_line_hint": "蓝色下降趋势线"
}
```

`expected_line_type` 可用 `auto`、`horizontal`、`trendline`。响应包含 `line`、`confidence`、`image_geometry` 和 `ready_for_strategy`。

### `POST /strategy/watch`

创建策略：

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "chart_timezone": "UTC",
  "direction": "LONG",
  "notional_usdt": 100,
  "leverage": 2,
  "mode": "simulate",
  "entry_line": {"kind": "horizontal", "price": 70000},
  "stop_line": {"kind": "trendline", "ts1": 1760000000000, "price1": 65000, "ts2": 1760100000000, "price2": 65500}
}
```

`stop_line` 可省略或设为 `null`。这种策略入场后不会自动触发止损，需手动处理仓位。

趋势线在两个锚点外也按同一斜率延伸。创建接口会获取实时合约价并验证止损方向。

其他接口：

- `GET /strategy/watch`：最近 50 条策略
- `GET /strategy/watch/{strategy_id}`：策略详情
- `DELETE /strategy/watch/{strategy_id}`：取消无持仓策略
- `DELETE /strategy/watch/{strategy_id}?close_position=true`：取消并平仓
- `DELETE /strategy/watch/{strategy_id}?close_position=false`：停止监控但保留仓位
- `GET /market/klines?symbol=BTCUSDT&timeframe=1h&limit=300`：合约 K 线代理

## 实盘保护规则

- 仅支持 Binance One-way Mode；Hedge Mode 会拒绝启用。
- 使用 CROSSED 全仓并设置所选杠杆。
- 启用前要求该交易对没有已有持仓或未完成订单。
- 入场与退出使用确定的 `newClientOrderId`；请求结果不确定时按该 ID 查询，降低重复下单风险。
- 服务在 `entering` 或 `exiting` 中途重启时不会猜测订单结果，而是进入 `attention_required`。
- 已开仓策略重启后先核对实际持仓方向和数量，再恢复止损监控。
- 行情 WebSocket 中断时使用 REST；超过 5 秒无可用行情会记录 stale 事件，但不会依据过期价格触发。
- SQLite 让单实例能够恢复状态；它不是多副本协调器，不要同时运行多个共享账户的服务实例。

## 测试

```bash
python -m unittest -v test_strategy.py
python -m py_compile main.py
docker compose config --quiet
```

测试覆盖水平线/趋势线计算、LONG/SHORT 触发、截图识别映射、模拟入场止损、取消和持久化恢复等关键路径。
