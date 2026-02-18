# BTC Breakout

基于两点趋势线的突破检测工具，提供：
- `main.py`：FastAPI 服务，支持后台轮询任务

## 项目结构

- `main.py`：API 服务，支持创建/查询后台轮询任务
- `Dockerfile` / `docker-compose.yml`：容器化运行

## 快速开始

### 1) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Docker 启动 API（仅支持 Docker）

```bash
docker compose up -d --build
docker compose logs -f api
```

停止并清理：

```bash
docker compose down
```

## API 使用

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
- `LOG_LEVEL`：日志级别，默认 `INFO`

示例：

```bash
BINANCE_API_KEY=your_key \
BINANCE_API_SECRET=your_secret \
BINANCE_BASE_URL=https://api.binance.us \
docker compose up -d --build
```
