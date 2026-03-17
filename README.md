# BTC Breakout

基于两点趋势线的 BTC 突破监控工具，包含一个 FastAPI 后端、一个单页网页工作台，以及一个可选的 Telegram 截图识别机器人。

当前代码已经实现的核心能力：

- `POST /signal/watch`：创建后台轮询任务，按趋势线判断是否突破
- `GET /signal/watch/{job_id}`：查询任务状态与检查快照
- `POST /ai/recognize`：上传 TradingView 截图，走后端 OpenRouter 识别并补全 payload
- `GET /`、`/manual`、`/ai`：提供同一个 `1.html` 工作台，仅按路径切换页面模式
- `GET /healthz`、`HEAD /healthz`：健康检查
- 可选 Telegram 机器人：复用同一套截图识别逻辑

## 项目结构

- `main.py`：FastAPI 服务、后台监控线程、OpenRouter 识别、Telegram 机器人
- `1.html`：前端工作台，后端根路径直接返回这个文件
- `Dockerfile`：容器镜像构建
- `docker-compose.yml`：单容器启动示例
- `OPENROUTER_PROMPT.md`：截图识别 prompt 说明

## 快速开始

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Docker Compose

```bash
docker compose up -d --build
docker compose logs -f api
```

访问地址：

- 工作台首页：`http://127.0.0.1:8000/`
- 手动画线页：`http://127.0.0.1:8000/manual`
- AI 识别页：`http://127.0.0.1:8000/ai`
- Swagger 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/healthz`

如果前面挂了 Nginx、宝塔、1Panel、云负载均衡等代理，健康检查建议直接指向 `GET /healthz` 或 `HEAD /healthz`。

停止容器：

```bash
docker compose down
```

如果同时配置了 `OPENROUTER_API_KEY` 和 `TELEGRAM_BOT_TOKEN`，服务启动后会自动开启 Telegram 长轮询机器人。

## 网页端工作台

网页端有两种入口，但最终都会生成同一种 `/signal/watch` payload。

### 手动画线

- 在左侧 K 线图上点击两次，生成趋势线锚点
- 默认会吸附到当前 K 线 `high/low`
- 按住 `Shift` 可临时关闭吸附
- 支持滚轮缩放，支持拖动画布平移
- 导出后会写入右侧当前 payload，再提交到 `/signal/watch`

### AI 截图识别

- 上传 TradingView 截图后，前端会请求后端 `POST /ai/recognize`
- 后端会读取 `OPENROUTER_API_KEY`、`OPENROUTER_MODEL` 调用 OpenRouter
- 当前默认模型是 `openai/gpt-5.3-codex`
- 请求里会启用 OpenRouter `web` 联网搜索和 `response-healing` 插件
- 后端会先让模型识别粗锚点，再结合当前 `symbol/timeframe` K 线做两步修复：
  - 根据截图里的日期级线索尽量补齐 `ts1/ts2`
  - 把锚点重新吸附到距离粗趋势线最近的两根 K 线 `high/low`
- 识别成功后，前端会把 payload 回填并尝试回画到左侧图表

AI 页面共享参数：

- `symbol`
- `timeframe`
- `usd_amount`
- `mode`
- `chart_timezone`
- `target_line_hint`

说明：

- `chart_timezone` 必须是 IANA 时区名，例如 `UTC`、`Asia/Shanghai`
- `target_line_hint` 用来提示模型识别哪一条线，例如 `蓝色下降压力线`
- 网页端截图识别不再要求浏览器直接持有 OpenRouter Key
- 如果后端未配置 `OPENROUTER_API_KEY`，`/ai/recognize` 会直接报错
- 左侧图表 K 线预览由浏览器直接请求 `https://api.binance.us/api/v3/klines`，这部分不走后端 `BINANCE_BASE_URL`

## `POST /ai/recognize`

请求体字段：

- `image_data_url`：Base64 Data URL，例如 `data:image/png;base64,...`
- `symbol`：默认 `BTCUSDT`
- `timeframe`：默认 `1h`
- `usd_amount`：默认 `100`
- `mode`：`simulate` 或 `live`
- `chart_timezone`：默认 `UTC`
- `target_line_hint`：可选

最小请求示例：

```bash
curl -sS -X POST 'http://127.0.0.1:8000/ai/recognize' \
  -H 'Content-Type: application/json' \
  -d '{
    "symbol": "BTCUSDT",
    "timeframe": "1h",
    "usd_amount": 100,
    "mode": "simulate",
    "chart_timezone": "UTC",
    "target_line_hint": "蓝色下降压力线",
    "image_data_url": "data:image/png;base64,<...>"
  }'
```

响应结构要点：

- `ready_for_api`：是否已经可直接提交到 `/signal/watch`
- `confidence`：`0` 到 `1`
- `trendline_kind`：`descending_resistance`、`ascending_support`、`flat`、`unknown`
- `api_payload`：最终可提交的监控 payload
- `anchors`：模型识别到的两个锚点
- `recovery`：如果后端做了日期恢复或最近 K 线吸附，会写这里
- `notes`：补充说明或需要人工复核的提示

## Telegram 机器人

启动条件：

- `OPENROUTER_API_KEY` 已配置
- `TELEGRAM_BOT_TOKEN` 已配置

可选限制：

- `TELEGRAM_ALLOWED_CHAT_IDS=123456789,987654321`

支持命令：

- `/help`
- `/config`
- `/showconfig`
- `/resetconfig`
- `/last`
- `/confirm`
- `/cancel`
- `/cancelall`

图片 caption 或 `/config` 支持的参数：

```text
symbol=BTCUSDT
timeframe=1h
usd_amount=100
mode=simulate
chart_timezone=UTC
target_line_hint=蓝色下降压力线
```

补充说明：

- `/config` 既支持多行 `key=value`，也支持 JSON 对象
- `/config`、`/showconfig`、`/resetconfig` 都作用于当前 chat 的默认参数
- 机器人会复用后端同一套 OpenRouter prompt、日期恢复和最近 K 线吸附逻辑
- 当识别出完整 `api_payload` 时，机器人会先回一张重画后的趋势线预览图和待确认参数
- 只有在你点击“确认提交”或发送 `/confirm` 后，机器人才会真正提交到 `/signal/watch`
- 如果要放弃当前待确认结果，可以发送 `/cancel`
- 如果要停止当前 chat 通过 Telegram 创建的所有监控任务，并同时清掉待确认结果，可以发送 `/cancelall`
- 建议用原图文件发送截图，避免 Telegram 压缩

## `POST /signal/watch`

这个接口会创建一个后台线程任务，立即返回 `job_id`，然后在后台持续轮询价格。

请求字段：

- `ts1`、`price1`、`ts2`、`price2`：趋势线两个锚点
- `symbol`：默认 `BTCUSDT`
- `qty` 或 `usd_amount`：二选一
- `mode`：`simulate` 或 `live`
- `current_price`：可选；不传则自动拉取
- `current_ts`：可选；不传则自动生成当前时间
- `base_url`：可选；覆盖后端 `BINANCE_BASE_URL`
- `interval_seconds`：轮询间隔，默认 `15`
- `max_checks`：最大检查次数，`null` 表示无限
- `stop_on_breakout`：触发后是否停止，默认 `true`
- `notify_url`：可选；覆盖 Bark 推送地址

约束：

- `qty` 和 `usd_amount` 不能同时传，也不能同时缺失
- `ts1` 与 `ts2` 不能相等
- 当 `max_checks=null` 时，必须 `stop_on_breakout=true`

判定逻辑：

- 下降趋势线：当前价格高于趋势线时返回 `BUY`
- 上升趋势线：当前价格低于趋势线时返回 `SELL`
- 水平线：始终返回 `NONE`
- 未突破时返回 `NONE`

`mode=live` 时的行为：

- 会在首次检测到突破时提交 Binance `MARKET` 订单
- 下单地址是 `${BINANCE_BASE_URL}/api/v3/order`
- 需要 `BINANCE_API_KEY` 和 `BINANCE_API_SECRET`

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
    "mode": "simulate",
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

## `GET /signal/watch/{job_id}`

```bash
curl -sS 'http://127.0.0.1:8000/signal/watch/<job_id>'
```

状态流转：

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

响应里会包含：

- 任务基础信息
- `checks_run`
- `last_snapshot`
- `error`
- `result`

`result.snapshots` 会记录每次检查的：

- 当前时间
- 当前价格
- 当前趋势线价格
- 价差与价差百分比
- 趋势方向
- 本次动作和原因

## 环境变量

- `BINANCE_BASE_URL`：后端行情与下单 API 根地址，默认 `https://api.binance.us`
- `BINANCE_API_KEY`：`mode=live` 所需
- `BINANCE_API_SECRET`：`mode=live` 所需
- `BARK_NOTIFY_URL`：默认 Bark 推送地址
- `OPENROUTER_API_KEY`：后端 AI 识别和 Telegram 机器人所需
- `OPENROUTER_API_URL`：可选，自定义 OpenRouter 接口地址
- `OPENROUTER_MODEL`：默认 `openai/gpt-5.3-codex`
- `AI_DEFAULT_SYMBOL`：Telegram 默认 `symbol`
- `AI_DEFAULT_TIMEFRAME`：Telegram 默认 `timeframe`
- `AI_DEFAULT_USD_AMOUNT`：Telegram 默认 `usd_amount`
- `AI_DEFAULT_MODE`：Telegram 默认 `mode`
- `AI_DEFAULT_CHART_TIMEZONE`：Telegram 默认 `chart_timezone`
- `AI_DEFAULT_LINE_HINT`：Telegram 默认 `target_line_hint`
- `TELEGRAM_BOT_TOKEN`：Telegram bot token
- `TELEGRAM_ALLOWED_CHAT_IDS`：允许使用机器人的 chat id 白名单
- `TELEGRAM_POLL_TIMEOUT_SECONDS`：Telegram 长轮询超时，默认 `60`
- `LOG_LEVEL`：默认 `INFO`
- `CORS_ALLOW_ORIGINS`：默认 `*`

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

## 当前实现限制

- `WATCH_JOBS` 保存在内存里，服务重启后任务状态会全部丢失
- Telegram 当前 chat 的默认参数和最近一次识别结果也只保存在内存里
- 后台监控依赖进程内线程，不适合多副本共享状态；如果做多实例部署，查询任务状态必须回到原始实例
- 当前代码里存在内置的 `DEFAULT_BARK_NOTIFY_URL` fallback；如果请求里没传 `notify_url`，环境变量也为空，仍会回退到代码默认值
