# BTC Breakout

一个面向 Binance USDⓈ-M Futures 的手工画线策略工作台。打开全屏 K 线图后，直接在图表上绘制趋势线或水平线；系统按手工线的价格函数计算延长线和突破 K 线，确认后可进入模拟或实盘策略监控。

## 主要能力

- Vue 3 + Lightweight Charts 黑白 K 线图（上涨空心、下跌实心），关闭背景网格线，支持交易对、1m–1w 周期、缩放、平移和刷新
- 每秒获取实时价格，更新当前未收盘 K 线、当前价格、选中线价格和突破状态；期货行情受限时自动使用 Spot 参考价
- 图表内显示独立的“实时”价格线和价格轴标签，随最新价格实时移动
- SVG 矢量画线层，图表平移、缩放和拖到未来空白时保持趋势线可见
- 手工趋势线与水平线，多条线可同时保存
- 趋势线按两个锚点的原始斜率自动延长到当前可视区和未来时间
- 每条线独立选择向上或向下突破方向
- 按 K 线影线触碰计算历史首次突破，并持续刷新实时 K 线结果
- 线条可以分配为入场线或止损线，保留现有 LONG/SHORT 模拟和实盘监控
- SQLite 保存策略、事件和成交状态；重启后恢复监控
- Bark 开仓、平仓和测试通知

系统只接受图表上的手工画线，不上传截图，也不调用模型、Codex 或其他自动识别服务。

## 快速开始

```bash
npm --prefix frontend install
npm --prefix frontend run build
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

- 手工画线工作台：`http://127.0.0.1:8000/`
- Bark 通知设置：`http://127.0.0.1:8000/settings`
- OpenAPI：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`

## 使用流程

1. 选择交易对和 K 线周期，等待行情加载。
2. 点击“趋势线”后，依次点击第一、第二锚点；也可以按住拖动快速完成。默认开启“磁吸”，会贴合附近 K 线的开高低收；按 `Esc` 取消，选中线后按 `Delete` 删除，按 `←` / `→` 或卡片中的 `1K` 按钮逐根移动。或点击“水平线”后在图表上点击价格位置。
3. 在右侧线条卡片中选择突破方向，按需设置“入场”和“止损”角色。
4. 查看选中线的延长价格和历史首次突破 K 线。
5. 设置方向、名义仓位、杠杆和运行模式，确认后启用策略。

趋势线两个锚点会以 Unix 毫秒时间戳保存。趋势线在两个锚点之外始终按同一斜率延长；突破计算使用最高价/最低价影线，不要求收盘确认。

## 接口

### `GET /market/klines`

```text
/market/klines?symbol=BTCUSDT&timeframe=1h&limit=500
```

优先返回 Binance Futures K 线；受地域限制时会降级到 Spot 参考 K 线，并在每根数据中标记 `source=binance_spot_fallback`。

### `POST /strategy/watch`

创建策略：

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "chart_timezone": "Asia/Shanghai",
  "direction": "LONG",
  "notional_usdt": 100,
  "leverage": 30,
  "mode": "simulate",
  "entry_line": {"kind": "trendline", "ts1": 1760000000000, "price1": 70000, "ts2": 1760100000000, "price2": 70500},
  "stop_line": {"kind": "horizontal", "price": 68000}
}
```

趋势线会在服务端通过 `LineSpec.price_at()` 按斜率计算任意时间的线价。其他策略接口：

- `GET /strategy/watch`：最近 50 条策略
- `GET /strategy/watch/{strategy_id}`：策略详情
- `DELETE /strategy/watch/{strategy_id}`：取消策略
- `GET/PUT /settings/bark`：读取或保存 Bark 配置
- `POST /settings/bark/test`：发送测试通知

## 环境变量

### Binance Futures

- `BINANCE_FUTURES_BASE_URL`：默认 `https://fapi.binance.com`
- `BINANCE_FUTURES_WS_URL`：默认 `wss://fstream.binance.com/ws`
- `BINANCE_API_KEY`、`BINANCE_API_SECRET`：仅实盘需要
- `ENABLE_LIVE_FUTURES`：只有 `true/1/yes/on` 才允许实盘
- `STRATEGY_DB_PATH`：默认项目目录下 `strategy_state.db`；Compose 使用 `/data/strategy_state.db`

### 通用

- `BARK_NOTIFY_URL`：Bark 初始通知地址；网页 `/settings` 保存后以网页配置为准
- `CORS_ALLOW_ORIGINS`：默认 `*`
- `LOG_LEVEL`：默认 `INFO`

实盘示例：

```bash
BINANCE_API_KEY=your_key \
BINANCE_API_SECRET=your_secret \
ENABLE_LIVE_FUTURES=true \
docker compose up -d --build
```

建议 API Key 只授予合约交易权限，不要授予提现权限，并使用 IP 白名单。

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
npm --prefix frontend run build
python -m unittest -v test_strategy.py
python -m py_compile main.py
docker compose config --quiet
```
