# BTC Breakout

基于两点趋势线的突破检测工具，提供：
- `main.py`：FastAPI 服务，支持后台轮询任务、后端 AI 截图识别和 Telegram 机器人收图识别
- `1.html`：网页端工作台，支持按页面模式切换手动画线和 AI 截图识别

## 项目结构

- `main.py`：API 服务，支持创建/查询后台轮询任务，并可按环境变量启动 Telegram 机器人
- `1.html`：前端页面，容器内由 FastAPI 根路径 `/` 直接提供
- `Dockerfile` / `docker-compose.yml`：容器化运行
- `OPENROUTER_PROMPT.md`：截图识别 prompt 说明

## 快速开始

### 1) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Docker Compose 启动

```bash
docker compose up -d --build
docker compose logs -f api
```

启动后访问：

- 手动画线页：`http://127.0.0.1:8000/manual`
- AI 识别页：`http://127.0.0.1:8000/ai`
- 根路径：`http://127.0.0.1:8000/`，默认进入手动画线页
- API：`http://127.0.0.1:8000/signal/watch`

如果同时配置了 `OPENROUTER_API_KEY` 和 `TELEGRAM_BOT_TOKEN`，容器启动后会自动开启 Telegram 长轮询机器人。

停止并清理：

```bash
docker compose down
```

## API 使用

## 网页端使用

网页端支持两条输入路径：

- 手动画线：在画布上点两次，导出最后一条趋势线为 API payload
- 截图识别：上传 TradingView 截图，浏览器直连 OpenRouter，并按统一的 `1h timeframe` 识别趋势线，再按距离趋势线最近的两根 K 线 high/low 吸附锚点；中途穿越其他 K 线也不会判失败，随后回填 payload 并回画到左侧 K 线图
- 当截图只有日刻度、模型无法直接给出精确时间戳时，前端会结合当前 `symbol/timeframe` 的 K 线，把日期级别线索补齐为最近 candle 的 `ts1/ts2`
- 当前默认配置统一为 `timeframe=1h`
- 截图识别默认模型已切到 `openai/gpt-5.3-codex`，并开启 OpenRouter `web` 联网搜索与 `response-healing` 插件

注意：

- 浏览器直连 OpenRouter 会把 API Key 暴露在当前页面环境里，只适合本机调试
- 正式部署建议把 OpenRouter 调用迁移到后端代理，或直接使用 Telegram 机器人方式

## Telegram 机器人识别

Telegram 机器人会复用 `1.html` 的同一套识别逻辑：

- 同样的 OpenRouter system prompt 和 JSON schema
- 同样的日期级时间线索恢复逻辑
- 同样的“按最近两根 K 线 high/low 吸附锚点”修复逻辑
- 返回同结构的 `api_payload`，可直接提交到 `/signal/watch`

启动条件：

- `OPENROUTER_API_KEY` 已配置
- `TELEGRAM_BOT_TOKEN` 已配置
- 可选：`TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321` 限制可用 chat id

机器人命令：

- `/help`：查看说明
- `/config`：设置当前 chat 的默认参数
- `/showconfig`：查看当前默认参数
- `/resetconfig`：重置为环境变量默认值
- `/last`：查看最近一次识别结果

图片 caption 或 `/config` 支持的参数：

```text
symbol=BTCUSDT
timeframe=1h
usd_amount=100
mode=simulate
chart_timezone=UTC
target_line_hint=蓝色下降压力线
```

建议：

- 尽量以“文件/原图”方式发送 TradingView 截图，避免 Telegram 压缩后影响识别
- 如果不写 caption，机器人会使用当前 chat 的默认参数

### 1) 创建后台轮询任务

接口：`POST /signal/watch`

请求示例：

```bash
curl -sS -X POST 'http://127.0.0.1:8000/signal/watch' \
  -H 'Content-Type: application/json' \
  -d '{
    "ts1": 1770332400000,
    "price1": 60110.73352601156,
    "ts2": 1771304400000,
    "price2": 67723.33514450867,
    "symbol": "BTCUSDT",
    "usd_amount": 100,
    "mode": "live",
    "interval_seconds": 15,
    "max_checks": 120,
    "stop_on_breakout": true
  }'
```

返回示例：

```json
{
  "job_id": "3f9f8e4dc94d4f1dadfd5b52f9f4b16c",
  "status": "queued",
  "created_ts": 1771305912032
}
```

### 2) 查询任务状态

接口：`GET /signal/watch/{job_id}`

```bash
curl -sS 'http://127.0.0.1:8000/signal/watch/<job_id>'
```

任务会经历 `queued -> running -> completed/failed`。

## 请求参数说明

通用参数（API）：
- `ts1`, `price1`, `ts2`, `price2`：构建趋势线的两个点
- `symbol`：交易对，如 `BTCUSDT`
- `qty` 或 `usd_amount`：仓位参数（二选一）
- `current_price`（可选）：触发价格；不传则自动拉取
- `current_ts`（可选）：触发时间（毫秒）；不传则自动生成
- `base_url`（可选）：行情接口基地址

轮询参数（仅 API `/signal/watch`）：
- `interval_seconds`：轮询间隔，默认 `15`
- `max_checks`：最大检查次数，`null` 表示无限
- `stop_on_breakout`：出现突破后是否停止，默认 `true`
- `notify_url`：Bark 推送地址，可覆盖环境变量

约束：
- 若 `max_checks=null`，则必须 `stop_on_breakout=true`。
- `mode="live"` 时，检测到突破会提交 Binance `MARKET` 真单。
- `mode="simulate"` 时，只做信号判断，不会下单。

## 返回字段说明

关键字段：
- `action`：`BUY` / `SELL` / `NONE`
- `reason`：判定原因
- `trigger_price`：当前触发价格
- `line_price`：当前时间点的趋势线价格
- `price_gap` / `price_gap_pct`：价差及百分比
- `trend_direction`：`ascending` / `descending` / `flat`
- `price_source`：`provided` / `auto`

## 环境变量

- `BINANCE_BASE_URL`：行情 API 根地址，默认 `https://api.binance.us`
- `BINANCE_API_KEY`：`mode="live"` 下单所需 API Key
- `BINANCE_API_SECRET`：`mode="live"` 下单所需 API Secret
- `BARK_NOTIFY_URL`：默认 Bark 推送地址
- `OPENROUTER_API_KEY`：后端 AI 识别和 Telegram 机器人所需 OpenRouter Key
- `OPENROUTER_MODEL`：后端 AI 识别模型，默认 `openai/gpt-5.3-codex`
- `AI_DEFAULT_SYMBOL`：Telegram 默认 `symbol`，默认 `BTCUSDT`
- `AI_DEFAULT_TIMEFRAME`：Telegram 默认 `timeframe`，默认 `1h`
- `AI_DEFAULT_USD_AMOUNT`：Telegram 默认 `usd_amount`，默认 `100`
- `AI_DEFAULT_MODE`：Telegram 默认 `mode`，默认 `simulate`
- `AI_DEFAULT_CHART_TIMEZONE`：Telegram 默认 `chart_timezone`，默认 `UTC`
- `AI_DEFAULT_LINE_HINT`：Telegram 默认 `target_line_hint`
- `TELEGRAM_BOT_TOKEN`：Telegram 机器人 token
- `TELEGRAM_ALLOWED_CHAT_IDS`：允许使用机器人的 chat id 白名单，逗号分隔；留空表示不限制
- `TELEGRAM_POLL_TIMEOUT_SECONDS`：Telegram 长轮询超时，默认 `60`
- `LOG_LEVEL`：日志级别，默认 `INFO`
- `CORS_ALLOW_ORIGINS`：允许的跨域来源，默认 `*`

示例：

```bash
BINANCE_API_KEY=your_key \
BINANCE_API_SECRET=your_secret \
BINANCE_BASE_URL=https://api.binance.us \
OPENROUTER_API_KEY=sk-or-... \
TELEGRAM_BOT_TOKEN=123456:ABCDEF \
TELEGRAM_ALLOWED_CHAT_IDS=123456789 \
docker compose up -d --build
```
