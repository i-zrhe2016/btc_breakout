from __future__ import annotations

import base64
import json
import hashlib
import hmac
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator


app = FastAPI(title="Trendline Breakout API", version="0.1.0")


DEFAULT_BARK_NOTIFY_URL = "https://api.day.app/j32eBocVfwx6kvf8xr452K/"
DEFAULT_BINANCE_BASE_URL = "https://api.binance.us"
DEFAULT_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.3-codex"
TELEGRAM_API_BASE_URL = "https://api.telegram.org"
LOG_LEVEL = str(os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trendline_api")
ROOT_DIR = Path(__file__).resolve().parent
WEB_ENTRY = ROOT_DIR / "1.html"
INTERVAL_TO_MS = {
    "1m": 60 * 1000,
    "3m": 3 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "8h": 8 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "3d": 3 * 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}
TRENDLINE_KINDS = {"descending_resistance", "ascending_support", "flat", "unknown"}
DATA_URL_IMAGE_PATTERN = re.compile(
    r"^data:(?P<mime>[-\w.+/]+)(?:;charset=[^;,]+)?;base64,(?P<payload>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
OPENROUTER_SYSTEM_PROMPT = "\n".join(
    [
        "你是一个严格的 TradingView 趋势线识别器。你的唯一任务，是从用户提供的一张图表截图中识别目标趋势线的两个锚点，并输出严格 JSON，供后端 API 直接调用。",
        "",
        "规则：",
        "1. 只识别人工绘制的趋势线，不要把均线、通道、中轴线、十字光标、订单线、价格标记当成趋势线。",
        "2. 先判断趋势线最贴近的是哪两根 K 线，再把这两根 K 线的高点/低点当作锚点；不要直接取趋势线延长后碰到屏幕左右边界的位置。",
        "3. ts1 必须小于 ts2，如果识别顺序反了，自动交换。",
        "4. ts1 和 ts2 必须是 Unix 毫秒时间戳。",
        "5. price1 和 price2 必须是数字，不要输出字符串。",
        "6. 优先使用用户给出的 default_symbol、usd_amount、mode、expected_timeframe、chart_timezone，不要自行改写。",
        "7. 时间应尽量对齐到对应 K 线开盘时间；价格优先读取锚点对应 K 线的 high/low，支撑线优先 low，压力线优先 high。",
        "8. 即使无法精确恢复 Unix 毫秒时间戳，也必须在 anchors[*].time_iso 里填写你能识别到的最细粒度时间线索：优先完整 ISO 时间；次选 YYYY-MM-DDTHH:mm；再次选 YYYY-MM-DD。不要因为只有日刻度就把 time_iso 留空。",
        "9. 如果图中存在多条人工趋势线，优先识别 target_line_hint 指定的那条；如果未指定，则选择最明显、最长、颜色最突出的单条斜向趋势线，并在 notes 说明依据。",
        "10. 只有在连近似日期/时间线索都无法给出，或者价格也无法识别时，才返回 ready_for_api=false，并把无法确认的字段设为 null。",
        "11. 如果只能识别到日期，允许 api_payload.ts1/ts2 暂时为 null，但 anchors[*].time_iso 必须保留日期级别信息供前端二次对齐。",
        "12. 趋势线穿越其他 K 线不算问题，不要因为中途穿越了若干根 K 线就放弃识别；仍然按距离趋势线最近的两根候选 K 线取锚点。",
        "13. 只输出 JSON 对象，不要输出 Markdown、解释、代码块。",
        "",
        "返回 JSON 必须包含：ready_for_api、confidence、symbol、timeframe、chart_timezone、trendline_kind、api_payload、anchors、notes。",
        "trendline_kind 只能是 descending_resistance、ascending_support、flat、unknown。",
        "api_payload 必须只包含 ts1、price1、ts2、price2、symbol、usd_amount、mode、interval_seconds、max_checks、stop_on_breakout。",
    ]
)
OPENROUTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "trendline_signal_payload",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ready_for_api",
            "confidence",
            "symbol",
            "timeframe",
            "chart_timezone",
            "trendline_kind",
            "api_payload",
            "anchors",
            "notes",
        ],
        "properties": {
            "ready_for_api": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "timeframe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "chart_timezone": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "trendline_kind": {
                "type": "string",
                "enum": ["descending_resistance", "ascending_support", "flat", "unknown"],
            },
            "api_payload": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "ts1",
                    "price1",
                    "ts2",
                    "price2",
                    "symbol",
                    "usd_amount",
                    "mode",
                    "interval_seconds",
                    "max_checks",
                    "stop_on_breakout",
                ],
                "properties": {
                    "ts1": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "price1": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "ts2": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "price2": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "usd_amount": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "mode": {"anyOf": [{"type": "string", "enum": ["simulate", "live"]}, {"type": "null"}]},
                    "interval_seconds": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "max_checks": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "stop_on_breakout": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                },
            },
            "anchors": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "time_iso", "price"],
                    "properties": {
                        "label": {"type": "string", "enum": ["p1", "p2"]},
                        "time_iso": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    },
                },
            },
            "notes": {"type": "string"},
        },
    },
}


def get_cors_origins() -> list[str]:
    raw = str(os.getenv("CORS_ALLOW_ORIGINS") or "*").strip()
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


JOB_STATUS = Literal["queued", "running", "completed", "failed"]


class TrendlineRequest(BaseModel):
    ts1: int = Field(..., description="Unix ms timestamp for point 1")
    price1: float = Field(..., gt=0)
    ts2: int = Field(..., description="Unix ms timestamp for point 2")
    price2: float = Field(..., gt=0)
    current_ts: Optional[int] = Field(None, description="Unix ms timestamp for trigger price")
    current_price: Optional[float] = Field(None, gt=0, description="Trigger price (auto fetched when omitted)")
    symbol: str = Field("BTCUSDT", min_length=3)
    qty: Optional[float] = Field(None, gt=0, description="Order quantity in base asset")
    usd_amount: Optional[float] = Field(None, gt=0, description="Position size in USD (used to derive qty)")
    base_url: Optional[str] = Field(None, description="Binance API base URL")
    mode: Literal["simulate", "live"] = "simulate"

    @field_validator("ts2")
    @classmethod
    def ts2_after_ts1(cls, v: int, info):
        ts1 = info.data.get("ts1")
        if ts1 is not None and v == ts1:
            raise ValueError("ts2 must differ from ts1")
        return v


@dataclass
class Trendline:
    slope: float
    intercept: float

    def price_at(self, ts: int) -> float:
        return self.slope * ts + self.intercept


class OrderDecision(BaseModel):
    action: Literal["BUY", "SELL", "NONE"]
    reason: str
    symbol: str
    qty: float
    usd_amount: float
    current_ts: int
    current_ts_iso: str
    trigger_price: float
    line_price: float
    price_gap: float
    price_gap_pct: float
    slope: float
    trend_direction: Literal["ascending", "descending", "flat"]
    breakout_condition: str
    price_source: Literal["provided", "auto"]


class SignalCheckSnapshot(BaseModel):
    check_index: int
    current_ts: int
    current_ts_iso: str
    current_price: float
    line_price: float
    price_gap: float
    price_gap_pct: float
    trend_direction: Literal["ascending", "descending", "flat"]
    action: Literal["BUY", "SELL", "NONE"]
    reason: str


class SignalWatchRequest(TrendlineRequest):
    interval_seconds: int = Field(15, ge=1, le=3600, description="Seconds between checks")
    max_checks: Optional[int] = Field(None, ge=1, le=1000000, description="Maximum number of checks; null means unlimited")
    stop_on_breakout: bool = Field(True, description="Stop polling when BUY/SELL is detected")
    notify_url: Optional[str] = Field(
        None,
        description="Bark push endpoint, e.g. https://api.day.app/<key>/",
    )


class AiRecognitionInputs(BaseModel):
    symbol: str = Field("BTCUSDT", min_length=3)
    timeframe: str = Field("1h", min_length=1)
    usd_amount: float = Field(100, gt=0)
    mode: Literal["simulate", "live"] = "simulate"
    chart_timezone: str = Field("UTC", min_length=1)
    target_line_hint: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol is required")
        return value

    @field_validator("timeframe")
    @classmethod
    def normalize_timeframe(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timeframe is required")
        return value

    @field_validator("chart_timezone")
    @classmethod
    def normalize_chart_timezone(cls, value: str) -> str:
        value = value.strip() or "UTC"
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid chart_timezone: {value}") from exc
        return value

    @field_validator("target_line_hint")
    @classmethod
    def normalize_target_line_hint(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AiRecognitionRequest(AiRecognitionInputs):
    image_data_url: str = Field(..., min_length=1)


class SignalWatchResult(BaseModel):
    symbol: str
    interval_seconds: int
    max_checks: Optional[int]
    started_ts: int
    ended_ts: int
    duration_seconds: float
    checks_run: int
    breakout_detected: bool
    breakout_action: Optional[Literal["BUY", "SELL"]]
    last_action: Literal["BUY", "SELL", "NONE"]
    snapshots: list[SignalCheckSnapshot]


class SignalWatchJobAccepted(BaseModel):
    job_id: str
    status: JOB_STATUS
    created_ts: int


class SignalWatchJobStatus(BaseModel):
    job_id: str
    status: JOB_STATUS
    symbol: str
    interval_seconds: int
    max_checks: Optional[int]
    stop_on_breakout: bool
    created_ts: int
    started_ts: Optional[int]
    ended_ts: Optional[int]
    checks_run: int
    last_snapshot: Optional[SignalCheckSnapshot]
    error: Optional[str]
    result: Optional[SignalWatchResult]


@dataclass
class WatchJobState:
    job_id: str
    payload: SignalWatchRequest
    status: JOB_STATUS
    created_ts: int
    started_ts: Optional[int] = None
    ended_ts: Optional[int] = None
    checks_run: int = 0
    last_snapshot: Optional[SignalCheckSnapshot] = None
    error: Optional[str] = None
    result: Optional[SignalWatchResult] = None


WATCH_JOBS: dict[str, WatchJobState] = {}
WATCH_JOBS_LOCK = threading.Lock()
TELEGRAM_CHAT_SETTINGS: dict[int, AiRecognitionInputs] = {}
TELEGRAM_LAST_RESULTS: dict[int, dict[str, Any]] = {}
TELEGRAM_STATE_LOCK = threading.Lock()
TELEGRAM_BOT_THREAD: Optional[threading.Thread] = None
TELEGRAM_BOT_STOP_EVENT = threading.Event()


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 30,
) -> Any:
    request_headers = {"User-Agent": "trendline-api/0.1"}
    if headers:
        request_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    req = Request(url, data=data, method=method, headers=request_headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        detail = f"HTTP {exc.code} {exc.reason}"
        if body:
            detail = f"{detail}; body={body}"
        raise RuntimeError(detail) from exc

    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON response from {url}") from exc


def request_bytes(url: str, *, headers: Optional[dict[str, str]] = None, timeout: int = 30) -> tuple[bytes, str]:
    request_headers = {"User-Agent": "trendline-api/0.1"}
    if headers:
        request_headers.update(headers)
    req = Request(url, headers=request_headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get_content_type()


def get_zoneinfo(name: Optional[str]) -> ZoneInfo:
    value = str(name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown time zone %s; falling back to UTC", value)
        return ZoneInfo("UTC")


def interval_to_ms(interval: Optional[str]) -> Optional[int]:
    return INTERVAL_TO_MS.get(str(interval or "").strip())


def normalize_mode_value(mode: Any) -> Optional[str]:
    value = str(mode or "").strip().lower()
    if value in {"simulate", "live"}:
        return value
    return None


def to_finite_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def to_int(value: Any) -> Optional[int]:
    number = to_finite_number(value)
    return None if number is None else int(number)


def get_default_ai_inputs() -> AiRecognitionInputs:
    raw_usd_amount = os.getenv("AI_DEFAULT_USD_AMOUNT") or "100"
    try:
        usd_amount = float(raw_usd_amount)
    except ValueError:
        logger.warning("Invalid AI_DEFAULT_USD_AMOUNT=%s; using 100", raw_usd_amount)
        usd_amount = 100.0

    try:
        return AiRecognitionInputs(
            symbol=os.getenv("AI_DEFAULT_SYMBOL") or "BTCUSDT",
            timeframe=os.getenv("AI_DEFAULT_TIMEFRAME") or "1h",
            usd_amount=usd_amount,
            mode=normalize_mode_value(os.getenv("AI_DEFAULT_MODE")) or "simulate",
            chart_timezone=os.getenv("AI_DEFAULT_CHART_TIMEZONE") or "UTC",
            target_line_hint=os.getenv("AI_DEFAULT_LINE_HINT") or None,
        )
    except Exception as exc:
        logger.warning("Invalid AI default config; falling back to built-ins: %s", exc)
        return AiRecognitionInputs(
            symbol="BTCUSDT",
            timeframe="1h",
            usd_amount=100,
            mode="simulate",
            chart_timezone="UTC",
            target_line_hint=None,
        )


def build_openrouter_user_prompt(inputs: AiRecognitionInputs) -> str:
    return "\n".join(
        [
            "请分析这张 TradingView 截图，提取目标趋势线的两个锚点，并返回严格 JSON。",
            "",
            "前端已知参数：",
            f"- default_symbol: {inputs.symbol or 'BTCUSDT'}",
            f"- usd_amount: {inputs.usd_amount}",
            f"- mode: {inputs.mode or 'simulate'}",
            f"- expected_timeframe: {inputs.timeframe or 'null'}",
            f"- chart_timezone: {inputs.chart_timezone or 'UTC'}",
            f"- target_line_hint: {inputs.target_line_hint or 'null'}",
            "",
            "额外要求：",
            "- 先判断趋势线最贴近的两根 K 线分别是哪两根，再连接这两根 K 线的 high/low，不要直接取延长线边界。",
            "- 如果趋势线锚点吸附在 K 线高点/低点，优先读取该 K 线的 high/low 作为价格；上升支撑优先 low，下降压力优先 high。",
            "- 趋势线中途穿越其他 K 线也没关系，不要因此否定该趋势线；只需要找离趋势线最近的候选 K 线。",
            "- 时间请尽量对齐到对应 K 线的开盘时间。",
            "- 如果只能识别到日期，anchors[*].time_iso 也必须填写 YYYY-MM-DD，不要留空。",
            "- 如果截图只有日刻度，也先给出最接近的日期级别判断，前端会再对齐到最近 candle。",
            "- 只有在连近似日期都无法识别时，才返回 ready_for_api=false。",
            "- 只输出 JSON。",
        ]
    )


def decode_image_data_url(data_url: str) -> tuple[bytes, str]:
    value = str(data_url or "").strip()
    match = DATA_URL_IMAGE_PATTERN.match(value)
    if not match:
        raise ValueError("image_data_url 必须是 base64 data URL，例如 data:image/png;base64,...")

    mime = str(match.group("mime") or "").strip().lower()
    if not mime.startswith("image/"):
        raise ValueError("image_data_url 必须包含 image/* MIME type")

    encoded = re.sub(r"\s+", "", match.group("payload") or "")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("image_data_url 的 base64 内容无效") from exc
    if not image_bytes:
        raise ValueError("image_data_url 不能为空")
    return image_bytes, mime


def message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return ""


def extract_json_object(text: Any) -> dict[str, Any]:
    trimmed = str(text or "").strip()
    if not trimmed:
        raise ValueError("模型返回为空")

    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", trimmed, re.IGNORECASE)
    if code_block_match:
        block = code_block_match.group(1).strip()
        parsed = json.loads(block)
        if isinstance(parsed, dict):
            return parsed

    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(trimmed[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("模型返回中未找到 JSON 对象")


def validate_ai_payload(payload: dict[str, Any]) -> None:
    invalid: list[str] = []
    if not isinstance(payload, dict):
        raise ValueError("payload 不是对象")
    if not isinstance(payload.get("ts1"), int):
        invalid.append("ts1")
    if not isinstance(payload.get("ts2"), int):
        invalid.append("ts2")
    if payload.get("ts1") == payload.get("ts2"):
        invalid.append("ts1/ts2")
    price1 = to_finite_number(payload.get("price1"))
    price2 = to_finite_number(payload.get("price2"))
    if price1 is None or price1 <= 0:
        invalid.append("price1")
    if price2 is None or price2 <= 0:
        invalid.append("price2")
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        invalid.append("symbol")
    usd_amount = to_finite_number(payload.get("usd_amount"))
    if usd_amount is None or usd_amount <= 0:
        invalid.append("usd_amount")
    if normalize_mode_value(payload.get("mode")) is None:
        invalid.append("mode")
    if invalid:
        raise ValueError("payload 缺少或非法字段: " + ", ".join(invalid))


def sanitize_payload(raw_payload: Any, inputs: AiRecognitionInputs) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    ts1 = to_int(payload.get("ts1"))
    ts2 = to_int(payload.get("ts2"))
    price1 = to_finite_number(payload.get("price1"))
    price2 = to_finite_number(payload.get("price2"))

    if ts1 is not None and ts2 is not None and ts1 > ts2:
        ts1, ts2 = ts2, ts1
        price1, price2 = price2, price1

    return {
        "ts1": ts1,
        "price1": price1,
        "ts2": ts2,
        "price2": price2,
        "symbol": str(payload.get("symbol") or inputs.symbol or "").strip().upper(),
        "usd_amount": to_finite_number(payload.get("usd_amount")) if payload.get("usd_amount") is not None else inputs.usd_amount,
        "mode": normalize_mode_value(payload.get("mode")) or inputs.mode,
        "interval_seconds": 15,
        "max_checks": None,
        "stop_on_breakout": True,
    }


def normalize_ai_result(result: Any, inputs: AiRecognitionInputs) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("模型返回不是 JSON 对象")

    ready = bool(result.get("ready_for_api"))
    confidence_value = to_finite_number(result.get("confidence"))
    confidence = 0.0 if confidence_value is None else max(0.0, min(1.0, confidence_value))
    trendline_kind = result.get("trendline_kind")
    if trendline_kind not in TRENDLINE_KINDS:
        trendline_kind = "unknown"

    raw_anchors = result.get("anchors") if isinstance(result.get("anchors"), list) else []
    anchors: list[dict[str, Any]] = []
    for idx, anchor in enumerate(raw_anchors[:2]):
        anchor_data = anchor if isinstance(anchor, dict) else {}
        anchors.append(
            {
                "label": "p2" if anchor_data.get("label") == "p2" else ("p1" if idx == 0 else "p2"),
                "time_iso": anchor_data.get("time_iso") if isinstance(anchor_data.get("time_iso"), str) else None,
                "price": to_finite_number(anchor_data.get("price")),
            }
        )

    while len(anchors) < 2:
        anchors.append({"label": "p1" if not anchors else "p2", "time_iso": None, "price": None})

    normalized = {
        "ready_for_api": ready,
        "confidence": confidence,
        "symbol": result.get("symbol") if isinstance(result.get("symbol"), str) else None,
        "timeframe": result.get("timeframe") if isinstance(result.get("timeframe"), str) else None,
        "chart_timezone": result.get("chart_timezone") if isinstance(result.get("chart_timezone"), str) else None,
        "trendline_kind": trendline_kind,
        "api_payload": sanitize_payload(result.get("api_payload"), inputs),
        "anchors": anchors,
        "notes": result.get("notes") if isinstance(result.get("notes"), str) else "",
        "recovery": None,
    }

    if normalized["ready_for_api"]:
        try:
            validate_ai_payload(normalized["api_payload"])
        except Exception as exc:
            normalized["ready_for_api"] = False
            normalized["notes"] = "\n".join(
                item
                for item in [
                    normalized["notes"],
                    f"模型返回了 ready_for_api=true，但 payload 仍缺少完整时间锚点：{exc}",
                ]
                if item
            )

    return normalized


def fetch_klines(symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
    url_base = str(os.getenv("BINANCE_BASE_URL") or DEFAULT_BINANCE_BASE_URL).rstrip("/")
    query = urlencode({"symbol": symbol, "interval": timeframe, "limit": limit})
    url = f"{url_base}/api/v3/klines?{query}"
    logger.info("Fetching klines: symbol=%s timeframe=%s url=%s", symbol, timeframe, url)
    req = Request(url, headers={"User-Agent": "trendline-api/0.1"})
    with urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("unexpected klines response")
    return [
        {
            "ts": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "vol": float(item[5]),
        }
        for item in raw
    ]


def candle_parts_in_time_zone(ts_ms: int, time_zone: str) -> dict[str, int]:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(get_zoneinfo(time_zone))
    return {
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
    }


def zoned_date_time_to_utc_ms(parts: dict[str, int], time_zone: str) -> Optional[int]:
    try:
        dt = datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts.get("hour", 0)),
            int(parts.get("minute", 0)),
            tzinfo=get_zoneinfo(time_zone),
        )
    except Exception:
        return None
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def parse_approximate_time_hint(text: Any) -> Optional[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None

    iso_match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s](\d{1,2})(?::(\d{2}))?)?", raw)
    if iso_match:
        return {
            "raw": raw,
            "year": int(iso_match.group(1)),
            "month": int(iso_match.group(2)),
            "day": int(iso_match.group(3)),
            "hour": None if iso_match.group(4) is None else int(iso_match.group(4)),
            "minute": None if iso_match.group(5) is None else int(iso_match.group(5)),
        }

    chinese_match = re.search(r"(?:(\d{4})年)?\s*(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})(?:[:点时](\d{1,2}))?)?", raw)
    if chinese_match:
        return {
            "raw": raw,
            "year": None if chinese_match.group(1) is None else int(chinese_match.group(1)),
            "month": int(chinese_match.group(2)),
            "day": int(chinese_match.group(3)),
            "hour": None if chinese_match.group(4) is None else int(chinese_match.group(4)),
            "minute": None if chinese_match.group(5) is None else int(chinese_match.group(5)),
        }

    slash_match = re.search(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:\s+(\d{1,2})(?::(\d{2}))?)?", raw)
    if slash_match:
        return {
            "raw": raw,
            "year": None if slash_match.group(1) is None else int(slash_match.group(1)),
            "month": int(slash_match.group(2)),
            "day": int(slash_match.group(3)),
            "hour": None if slash_match.group(4) is None else int(slash_match.group(4)),
            "minute": None if slash_match.group(5) is None else int(slash_match.group(5)),
        }

    return None


def extract_approximate_dates_from_notes(notes: Any) -> list[str]:
    text = str(notes or "")
    return re.findall(
        r"(?:(?:\d{4}-\d{1,2}-\d{1,2})(?:[T\s]\d{1,2}(?::\d{2})?)?|(?:(?:\d{4})年)?\d{1,2}月\d{1,2}日(?:\s*\d{1,2}(?:[:点时]\d{1,2})?)?)",
        text,
    )


def assign_missing_years_to_hints(
    hints: list[Optional[dict[str, Any]]],
    candles: list[dict[str, Any]],
    time_zone: str,
) -> list[Optional[dict[str, Any]]]:
    if not hints or not candles:
        return hints

    years = sorted({candle_parts_in_time_zone(item["ts"], time_zone)["year"] for item in candles})
    if not years:
        return hints

    assigned: list[Optional[dict[str, Any]]] = []
    for hint in hints:
        if not hint or hint.get("year") is not None:
            assigned.append(hint)
            continue

        candidate_year = None
        for year in years:
            for candle in candles:
                parts = candle_parts_in_time_zone(candle["ts"], time_zone)
                if parts["year"] == year and parts["month"] == hint["month"] and parts["day"] == hint["day"]:
                    candidate_year = year
                    break
            if candidate_year is not None:
                break

        assigned.append({**hint, "year": candidate_year if candidate_year is not None else years[-1]})

    return assigned


def price_distance_for_anchor(candle: dict[str, Any], price: Optional[float], trendline_kind: str) -> float:
    if price is None:
        return 0.0
    if trendline_kind == "ascending_support":
        return abs(candle["low"] - price)
    if trendline_kind == "descending_resistance":
        return abs(candle["high"] - price)
    return min(
        abs(candle["low"] - price),
        abs(candle["high"] - price),
        abs(candle["open"] - price),
        abs(candle["close"] - price),
    )


def infer_anchor_timestamp(
    hint: Optional[dict[str, Any]],
    price: Optional[float],
    trendline_kind: str,
    time_zone: str,
    candles: list[dict[str, Any]],
) -> Optional[int]:
    if not hint or hint.get("year") is None or not candles:
        return None
    try:
        hint_day = datetime(hint["year"], hint["month"], hint["day"], tzinfo=timezone.utc)
    except ValueError:
        return None

    target_minute = None
    if hint.get("hour") is not None:
        target_minute = int(hint["hour"]) * 60 + int(hint.get("minute") or 0)

    best: Optional[dict[str, Any]] = None
    for candle in candles:
        parts = candle_parts_in_time_zone(candle["ts"], time_zone)
        day_score = abs(
            datetime(parts["year"], parts["month"], parts["day"], tzinfo=timezone.utc).timestamp()
            - hint_day.timestamp()
        ) / (24 * 60 * 60)
        if day_score > 7:
            continue

        minute_score = 0.0
        if target_minute is not None:
            minute_score = abs((parts["hour"] * 60 + parts["minute"]) - target_minute) / 60
        price_score = price_distance_for_anchor(candle, price, trendline_kind)
        total_score = day_score * 1000 + minute_score * 10 + price_score

        if best is None or total_score < best["total_score"]:
            best = {"candle": candle, "total_score": total_score, "day_score": day_score}

    if best is None or best["day_score"] > 1.5:
        return None
    return int(best["candle"]["ts"])


def synthesize_timestamp_from_hint(hint: Optional[dict[str, Any]], timeframe: str, time_zone: str) -> Optional[int]:
    if not hint or hint.get("year") is None:
        return None
    interval_ms = interval_to_ms(timeframe)
    if not interval_ms:
        return None

    hour = 0 if hint.get("hour") is None else int(hint["hour"])
    minute = 0 if hint.get("minute") is None else int(hint["minute"])
    interval_minutes = max(1, round(interval_ms / (60 * 1000)))
    total_minutes = hour * 60 + minute
    aligned_minutes = (total_minutes // interval_minutes) * interval_minutes
    return zoned_date_time_to_utc_ms(
        {
            "year": hint["year"],
            "month": hint["month"],
            "day": hint["day"],
            "hour": aligned_minutes // 60,
            "minute": aligned_minutes % 60,
        },
        time_zone,
    )


def derive_approximate_time_hints(normalized: dict[str, Any], candles: list[dict[str, Any]]) -> list[Optional[dict[str, Any]]]:
    time_zone = normalized.get("chart_timezone") or "UTC"
    base_hints = [parse_approximate_time_hint(anchor.get("time_iso")) for anchor in normalized["anchors"]]
    note_hints = assign_missing_years_to_hints(
        [parse_approximate_time_hint(item) for item in extract_approximate_dates_from_notes(normalized.get("notes"))],
        candles,
        time_zone,
    )
    combined = [base_hints[idx] or (note_hints[idx] if idx < len(note_hints) else None) for idx in range(2)]
    return assign_missing_years_to_hints(combined, candles, time_zone)


def maybe_recover_payload_from_approximate_times(
    normalized: dict[str, Any],
    candles: list[dict[str, Any]],
    inputs: AiRecognitionInputs,
) -> dict[str, Any]:
    if not normalized or not normalized.get("api_payload"):
        return normalized

    payload = sanitize_payload(normalized.get("api_payload"), inputs)
    hints = derive_approximate_time_hints(normalized, candles)
    time_zone = normalized.get("chart_timezone") or "UTC"
    timeframe = inputs.timeframe or normalized.get("timeframe") or "1h"

    if payload["ts1"] is None and hints[0]:
        payload["ts1"] = infer_anchor_timestamp(
            hints[0],
            payload["price1"] if payload["price1"] is not None else normalized["anchors"][0].get("price"),
            normalized.get("trendline_kind") or "unknown",
            time_zone,
            candles,
        )
        if payload["ts1"] is None:
            payload["ts1"] = synthesize_timestamp_from_hint(hints[0], timeframe, time_zone)

    if payload["ts2"] is None and hints[1]:
        payload["ts2"] = infer_anchor_timestamp(
            hints[1],
            payload["price2"] if payload["price2"] is not None else normalized["anchors"][1].get("price"),
            normalized.get("trendline_kind") or "unknown",
            time_zone,
            candles,
        )
        if payload["ts2"] is None:
            payload["ts2"] = synthesize_timestamp_from_hint(hints[1], timeframe, time_zone)

    if payload["price1"] is None and to_finite_number(normalized["anchors"][0].get("price")) is not None:
        payload["price1"] = to_finite_number(normalized["anchors"][0].get("price"))
    if payload["price2"] is None and to_finite_number(normalized["anchors"][1].get("price")) is not None:
        payload["price2"] = to_finite_number(normalized["anchors"][1].get("price"))

    recovered = (
        isinstance(payload["ts1"], int)
        and isinstance(payload["ts2"], int)
        and to_finite_number(payload["price1"]) is not None
        and to_finite_number(payload["price2"]) is not None
    )
    if not recovered:
        return normalized

    validate_ai_payload(payload)
    return {
        **normalized,
        "ready_for_api": True,
        "confidence": min(float(normalized.get("confidence") or 0), 0.79),
        "api_payload": payload,
        "recovery": {
            "usedApproximateTimeHint": True,
            "anchors": [hint.get("raw") if hint else None for hint in hints],
            "mode": "date-hint-to-candle-open",
        },
        "notes": "\n".join(
            item
            for item in [
                normalized.get("notes") or "",
                f"已根据截图中的近似日期线索补全 ts1/ts2；若对应日期超出当前已加载 K 线范围，则按当前 {timeframe} K 线对齐到该日期的 candle 开盘时间。请人工复核后再提交。",
            ]
            if item
        ),
    }


def get_series_interval_ms(candles: list[dict[str, Any]], timeframe: str) -> int:
    if len(candles) >= 2:
        delta = int(candles[1]["ts"]) - int(candles[0]["ts"])
        if delta > 0:
            return delta
    return interval_to_ms(timeframe) or 60 * 60 * 1000


def project_payload_price_at_ts(payload: dict[str, Any], ts: int) -> Optional[float]:
    if not isinstance(payload.get("ts1"), int) or not isinstance(payload.get("ts2"), int):
        return None
    price1 = to_finite_number(payload.get("price1"))
    price2 = to_finite_number(payload.get("price2"))
    if price1 is None or price2 is None or payload["ts1"] == payload["ts2"]:
        return None
    return price1 + (price2 - price1) * ((ts - payload["ts1"]) / (payload["ts2"] - payload["ts1"]))


def clamp_timestamp_to_series(ts: Optional[int], candles: list[dict[str, Any]]) -> Optional[int]:
    if ts is None or not candles:
        return None
    return max(min(ts, candles[-1]["ts"]), candles[0]["ts"])


def pick_anchor_price_from_candle(
    candle: Optional[dict[str, Any]],
    trendline_kind: str,
    reference_price: Optional[float],
) -> Optional[dict[str, Any]]:
    if candle is None:
        return None
    if trendline_kind == "ascending_support":
        return {"price": candle["low"], "source": "low"}
    if trendline_kind == "descending_resistance":
        return {"price": candle["high"], "source": "high"}
    reference = reference_price if reference_price is not None else (float(candle["high"]) + float(candle["low"])) / 2
    if abs(candle["high"] - reference) < abs(candle["low"] - reference):
        return {"price": candle["high"], "source": "high"}
    return {"price": candle["low"], "source": "low"}


def format_anchor_timestamp(ts: Optional[int], time_zone: str) -> Optional[str]:
    if not isinstance(ts, int):
        return None
    parts = candle_parts_in_time_zone(ts, time_zone)
    return f"{parts['year']:04d}-{parts['month']:02d}-{parts['day']:02d} {parts['hour']:02d}:{parts['minute']:02d}"


def find_nearest_trendline_candle(
    payload: dict[str, Any],
    normalized: dict[str, Any],
    hints: list[Optional[dict[str, Any]]],
    anchor_index: int,
    exclude_ts: Optional[int],
    candles: list[dict[str, Any]],
    inputs: AiRecognitionInputs,
) -> Optional[dict[str, Any]]:
    if not payload or not candles:
        return None

    interval_ms = get_series_interval_ms(candles, inputs.timeframe)
    raw_target_ts = payload["ts1"] if anchor_index == 0 else payload["ts2"]
    fallback_target_ts = candles[0]["ts"] if anchor_index == 0 else candles[-1]["ts"]
    target_ts = clamp_timestamp_to_series(raw_target_ts, candles) or fallback_target_ts
    other_raw_ts = payload["ts2"] if anchor_index == 0 else payload["ts1"]
    other_fallback_ts = candles[-1]["ts"] if anchor_index == 0 else candles[0]["ts"]
    other_ts = clamp_timestamp_to_series(other_raw_ts, candles) or other_fallback_ts
    midpoint_ts = (target_ts + other_ts) / 2
    target_price = payload["price1"] if anchor_index == 0 else payload["price2"]
    time_zone = normalized.get("chart_timezone") or "UTC"
    hint = hints[anchor_index] if anchor_index < len(hints) else None
    hint_day = None
    if hint and hint.get("year") is not None:
        try:
            hint_day = datetime(hint["year"], hint["month"], hint["day"], tzinfo=timezone.utc)
        except ValueError:
            hint_day = None

    candidates = []
    for candle in candles:
        if exclude_ts is not None and candle["ts"] == exclude_ts:
            continue
        if anchor_index == 0 and candle["ts"] > midpoint_ts + interval_ms * 2:
            continue
        if anchor_index == 1 and candle["ts"] < midpoint_ts - interval_ms * 2:
            continue
        candidates.append(candle)
    if not candidates:
        candidates = [candle for candle in candles if exclude_ts is None or candle["ts"] != exclude_ts]

    best = None
    for candle in candidates:
        projected_price = project_payload_price_at_ts(payload, candle["ts"])
        anchor = pick_anchor_price_from_candle(
            candle,
            normalized.get("trendline_kind") or "unknown",
            projected_price if projected_price is not None else target_price,
        )
        if not anchor or to_finite_number(anchor.get("price")) is None:
            continue

        price_reference = projected_price if projected_price is not None else (target_price if target_price is not None else anchor["price"])
        if price_reference not in (None, 0):
            price_gap_pct = abs(anchor["price"] - price_reference) / abs(price_reference) * 100
        else:
            price_gap_pct = 0.0
        time_distance_candles = abs(candle["ts"] - target_ts) / interval_ms
        score = price_gap_pct * 1000 + time_distance_candles * 0.35

        if hint_day is not None:
            parts = candle_parts_in_time_zone(candle["ts"], time_zone)
            candle_day = datetime(parts["year"], parts["month"], parts["day"], tzinfo=timezone.utc)
            score += abs((candle_day - hint_day).total_seconds()) / (24 * 60 * 60) * 0.4

        if best is None or score < best["score"]:
            best = {"candle": candle, "price": anchor["price"], "source": anchor["source"], "score": score}

    return best


def snap_payload_to_nearest_candles_once(
    payload: dict[str, Any],
    normalized: dict[str, Any],
    hints: list[Optional[dict[str, Any]]],
    candles: list[dict[str, Any]],
    inputs: AiRecognitionInputs,
) -> Optional[dict[str, Any]]:
    left = find_nearest_trendline_candle(payload, normalized, hints, 0, None, candles, inputs)
    right = find_nearest_trendline_candle(payload, normalized, hints, 1, left["candle"]["ts"] if left else None, candles, inputs)
    if not left or not right:
        return None

    snapped_payload = sanitize_payload(
        {
            **payload,
            "ts1": left["candle"]["ts"],
            "price1": left["price"],
            "ts2": right["candle"]["ts"],
            "price2": right["price"],
        },
        inputs,
    )
    if not isinstance(snapped_payload["ts1"], int) or not isinstance(snapped_payload["ts2"], int):
        return None
    if snapped_payload["ts1"] == snapped_payload["ts2"]:
        return None

    return {
        "payload": snapped_payload,
        "anchors": [
            {"label": "p1", "ts": snapped_payload["ts1"], "price": snapped_payload["price1"], "source": left["source"]},
            {"label": "p2", "ts": snapped_payload["ts2"], "price": snapped_payload["price2"], "source": right["source"]},
        ],
    }


def maybe_snap_payload_to_nearest_candles(
    normalized: dict[str, Any],
    candles: list[dict[str, Any]],
    inputs: AiRecognitionInputs,
) -> dict[str, Any]:
    if not normalized or not normalized.get("api_payload") or not candles:
        return normalized

    payload = sanitize_payload(normalized["api_payload"], inputs)
    try:
        validate_ai_payload(payload)
    except Exception:
        return normalized

    hints = derive_approximate_time_hints(normalized, candles)
    first_pass = snap_payload_to_nearest_candles_once(payload, normalized, hints, candles, inputs)
    second_pass = (
        snap_payload_to_nearest_candles_once(first_pass["payload"], normalized, hints, candles, inputs)
        if first_pass
        else None
    )
    snapped = second_pass or first_pass
    if not snapped:
        return normalized

    validate_ai_payload(snapped["payload"])
    changed = any(
        snapped["payload"][key] != payload[key]
        for key in ("ts1", "ts2", "price1", "price2")
    )
    if not changed:
        return normalized

    time_zone = normalized.get("chart_timezone") or "UTC"
    anchor_summary = "；".join(
        f"{anchor['label']}={format_anchor_timestamp(anchor['ts'], time_zone)} {anchor['source']} {anchor['price']}"
        for anchor in snapped["anchors"]
    )
    price_side = "high/low"
    if normalized.get("trendline_kind") == "descending_resistance":
        price_side = "high"
    elif normalized.get("trendline_kind") == "ascending_support":
        price_side = "low"

    return {
        **normalized,
        "ready_for_api": True,
        "api_payload": snapped["payload"],
        "recovery": {
            **(normalized.get("recovery") or {}),
            "snappedToNearestCandles": True,
            "anchorCandles": snapped["anchors"],
            "mode": (
                f"{normalized['recovery']['mode']}+nearest-candle-extrema"
                if normalized.get("recovery") and normalized["recovery"].get("mode")
                else "nearest-candle-extrema"
            ),
        },
        "notes": "\n".join(
            item
            for item in [
                normalized.get("notes") or "",
                f"已按当前 {inputs.timeframe} 框架中距离粗趋势线最近的两根 K 线重新吸附锚点，并连接对应 {price_side}；中途穿越其他 K 线不会被视为失败：{anchor_summary}。",
            ]
            if item
        ),
    }


def finalize_recognized_payload(
    normalized: dict[str, Any],
    candles: list[dict[str, Any]],
    inputs: AiRecognitionInputs,
) -> dict[str, Any]:
    return maybe_snap_payload_to_nearest_candles(
        maybe_recover_payload_from_approximate_times(normalized, candles, inputs),
        candles,
        inputs,
    )


def recognize_trendline_from_image(
    image_bytes: bytes,
    inputs: AiRecognitionInputs,
    *,
    image_content_type: str = "image/jpeg",
) -> dict[str, Any]:
    api_key = str(os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未配置")

    model = str(os.getenv("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL
    candles = fetch_klines(inputs.symbol, inputs.timeframe)
    image_data_url = "data:{mime};base64,{payload}".format(
        mime=image_content_type or "image/jpeg",
        payload=base64.b64encode(image_bytes).decode("ascii"),
    )
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "plugins": [
            {"id": "web", "engine": "native", "max_results": 5},
            {"id": "response-healing"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": OPENROUTER_RESPONSE_SCHEMA,
        },
        "messages": [
            {"role": "system", "content": OPENROUTER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_openrouter_user_prompt(inputs)},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ],
    }

    response_json = request_json(
        str(os.getenv("OPENROUTER_API_URL") or DEFAULT_OPENROUTER_API_URL).strip(),
        method="POST",
        payload=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120,
    )
    choices = response_json.get("choices") if isinstance(response_json, dict) else None
    if not choices:
        raise RuntimeError("OpenRouter 响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    parsed = None
    if isinstance(message, dict):
        parsed = message.get("parsed")
        if not isinstance(parsed, dict):
            parsed = extract_json_object(message_content_to_text(message.get("content")))
    normalized = normalize_ai_result(parsed, inputs)
    return finalize_recognized_payload(normalized, candles, inputs)

def compute_trendline(ts1: int, price1: float, ts2: int, price2: float) -> Trendline:
    slope = (price2 - price1) / (ts2 - ts1)
    intercept = price1 - slope * ts1
    return Trendline(slope=slope, intercept=intercept)


def ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def direction_from_slope(slope: float) -> Literal["ascending", "descending", "flat"]:
    if slope > 0:
        return "ascending"
    if slope < 0:
        return "descending"
    return "flat"


def fetch_latest_price(symbol: str, base_url: Optional[str]) -> float:
    url_base = str(base_url or os.getenv("BINANCE_BASE_URL") or DEFAULT_BINANCE_BASE_URL).rstrip("/")
    query = urlencode({"symbol": symbol})
    futures_url = f"{url_base}/fapi/v1/ticker/price?{query}"
    spot_url = f"{url_base}/api/v3/ticker/price?{query}"

    # Binance US supports spot API; Binance global futures supports fapi.
    url_candidates = [spot_url, futures_url] if "binance.us" in url_base else [futures_url, spot_url]

    last_err: Exception | None = None
    for url in url_candidates:
        try:
            logger.info("Fetching latest price: symbol=%s url=%s", symbol, url)
            req = Request(url, headers={"User-Agent": "trendline-api/0.1"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            price = float(data["price"])
            logger.info("Fetched latest price: symbol=%s price=%s", symbol, price)
            return price
        except Exception as e:
            logger.warning("Price fetch failed: symbol=%s url=%s error=%s", symbol, url, e)
            last_err = e

    raise RuntimeError(f"failed to fetch latest price from {url_base}: {last_err}")


def _format_order_qty(qty: float) -> str:
    # Binance expects a decimal string without scientific notation.
    return f"{qty:.12f}".rstrip("0").rstrip(".")


def place_live_order(
    payload: SignalWatchRequest,
    decision: OrderDecision,
) -> dict:
    api_key = (os.getenv("BINANCE_API_KEY") or "").strip()
    api_secret = (os.getenv("BINANCE_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("live mode requires BINANCE_API_KEY and BINANCE_API_SECRET")

    url_base = str(payload.base_url or os.getenv("BINANCE_BASE_URL") or DEFAULT_BINANCE_BASE_URL).rstrip("/")
    order_url = f"{url_base}/api/v3/order"
    ts_ms = int(time.time() * 1000)
    quantity = _format_order_qty(decision.qty)
    if not quantity:
        raise RuntimeError("computed order quantity is invalid")

    params = {
        "symbol": payload.symbol,
        "side": decision.action,
        "type": "MARKET",
        "quantity": quantity,
        "timestamp": str(ts_ms),
        "recvWindow": "5000",
    }
    query = urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    body = f"{query}&signature={signature}".encode("utf-8")
    req = Request(
        order_url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "trendline-api/0.1",
            "X-MBX-APIKEY": api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        logger.info(
            "Live order submitted: symbol=%s side=%s quantity=%s orderId=%s status=%s",
            payload.symbol,
            decision.action,
            quantity,
            result.get("orderId"),
            result.get("status"),
        )
        return result
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", "ignore")
        except Exception:
            err_body = ""
        logger.exception(
            "Live order submission failed: symbol=%s side=%s quantity=%s url=%s",
            payload.symbol,
            decision.action,
            quantity,
            order_url,
        )
        detail = f"HTTP {e.code} {e.reason}"
        if err_body:
            detail = f"{detail}; body={err_body}"
        raise RuntimeError(f"live order submission failed: {detail}") from e
    except Exception as e:
        logger.exception(
            "Live order submission failed: symbol=%s side=%s quantity=%s url=%s",
            payload.symbol,
            decision.action,
            quantity,
            order_url,
        )
        raise RuntimeError(f"live order submission failed: {e}") from e


def resolve_order_inputs(payload: TrendlineRequest) -> tuple[int, float, float, float, Literal["provided", "auto"]]:
    if payload.qty is None and payload.usd_amount is None:
        raise ValueError("One of qty or usd_amount must be provided")
    if payload.qty is not None and payload.usd_amount is not None:
        raise ValueError("Provide only one of qty or usd_amount")

    current_ts = payload.current_ts if payload.current_ts is not None else int(time.time() * 1000)

    if payload.current_price is None:
        current_price = fetch_latest_price(payload.symbol, payload.base_url)
        price_source: Literal["provided", "auto"] = "auto"
    else:
        current_price = payload.current_price
        price_source = "provided"

    if payload.usd_amount is not None:
        usd_amount = payload.usd_amount
        qty = usd_amount / current_price
    else:
        qty = payload.qty  # validated above
        usd_amount = qty * current_price

    logger.info(
        "Resolved order inputs: symbol=%s ts=%s current_price=%s qty=%s usd_amount=%s price_source=%s",
        payload.symbol,
        current_ts,
        current_price,
        qty,
        usd_amount,
        price_source,
    )
    return current_ts, current_price, qty, usd_amount, price_source


def build_decision(
    payload: TrendlineRequest,
    action: Literal["BUY", "SELL", "NONE"],
    reason: str,
    current_ts: int,
    current_price: float,
    qty: float,
    usd_amount: float,
    line_price: float,
    slope: float,
    price_source: Literal["provided", "auto"],
) -> OrderDecision:
    price_gap = current_price - line_price
    price_gap_pct = (price_gap / line_price * 100) if line_price != 0 else 0.0
    trend_direction = direction_from_slope(slope)
    if trend_direction == "descending":
        breakout_condition = "BUY when current_price > line_price"
    elif trend_direction == "ascending":
        breakout_condition = "SELL when current_price < line_price"
    else:
        breakout_condition = "No breakout condition for flat trendline"

    return OrderDecision(
        action=action,
        reason=reason,
        symbol=payload.symbol,
        qty=qty,
        usd_amount=usd_amount,
        current_ts=current_ts,
        current_ts_iso=ts_to_iso(current_ts),
        trigger_price=current_price,
        line_price=line_price,
        price_gap=price_gap,
        price_gap_pct=price_gap_pct,
        slope=slope,
        trend_direction=trend_direction,
        breakout_condition=breakout_condition,
        price_source=price_source,
    )


def decide_order(payload: TrendlineRequest) -> OrderDecision:
    logger.info(
        "Evaluating signal: symbol=%s mode=%s ts1=%s price1=%s ts2=%s price2=%s",
        payload.symbol,
        payload.mode,
        payload.ts1,
        payload.price1,
        payload.ts2,
        payload.price2,
    )
    current_ts, current_price, qty, usd_amount, price_source = resolve_order_inputs(payload)
    line = compute_trendline(payload.ts1, payload.price1, payload.ts2, payload.price2)
    line_price = line.price_at(current_ts)
    logger.info(
        "Computed trendline state: symbol=%s slope=%s line_price=%s current_price=%s",
        payload.symbol,
        line.slope,
        line_price,
        current_price,
    )

    if line.slope < 0:
        if current_price > line_price:
            decision = build_decision(
                payload=payload,
                action="BUY",
                reason="Breakout above descending trendline",
                current_ts=current_ts,
                current_price=current_price,
                qty=qty,
                usd_amount=usd_amount,
                line_price=line_price,
                slope=line.slope,
                price_source=price_source,
            )
            logger.info(
                "Decision made: symbol=%s action=%s reason=%s",
                decision.symbol,
                decision.action,
                decision.reason,
            )
            return decision
    elif line.slope > 0:
        if current_price < line_price:
            decision = build_decision(
                payload=payload,
                action="SELL",
                reason="Breakout below ascending trendline",
                current_ts=current_ts,
                current_price=current_price,
                qty=qty,
                usd_amount=usd_amount,
                line_price=line_price,
                slope=line.slope,
                price_source=price_source,
            )
            logger.info(
                "Decision made: symbol=%s action=%s reason=%s",
                decision.symbol,
                decision.action,
                decision.reason,
            )
            return decision
    else:
        decision = build_decision(
            payload=payload,
            action="NONE",
            reason="Flat trendline; no breakout logic",
            current_ts=current_ts,
            current_price=current_price,
            qty=qty,
            usd_amount=usd_amount,
            line_price=line_price,
            slope=line.slope,
            price_source=price_source,
        )
        logger.info(
            "Decision made: symbol=%s action=%s reason=%s",
            decision.symbol,
            decision.action,
            decision.reason,
        )
        return decision

    decision = build_decision(
        payload=payload,
        action="NONE",
        reason="No breakout detected",
        current_ts=current_ts,
        current_price=current_price,
        qty=qty,
        usd_amount=usd_amount,
        line_price=line_price,
        slope=line.slope,
        price_source=price_source,
    )
    logger.info(
        "Decision made: symbol=%s action=%s reason=%s",
        decision.symbol,
        decision.action,
        decision.reason,
    )
    return decision


def watch_signal(
    payload: SignalWatchRequest,
    on_snapshot: Optional[Callable[[SignalCheckSnapshot], None]] = None,
) -> SignalWatchResult:
    logger.info(
        "Signal watch started: symbol=%s interval_seconds=%s max_checks=%s stop_on_breakout=%s",
        payload.symbol,
        payload.interval_seconds,
        payload.max_checks,
        payload.stop_on_breakout,
    )
    snapshots: list[SignalCheckSnapshot] = []
    breakout_action: Optional[Literal["BUY", "SELL"]] = None
    live_order_submitted = False
    started_ts = int(time.time() * 1000)

    i = 0
    while True:
        if payload.max_checks is not None and i >= payload.max_checks:
            break

        check_payload = payload.model_copy(update={"current_ts": None, "current_price": None})
        decision = decide_order(check_payload)
        snapshot = SignalCheckSnapshot(
            check_index=i + 1,
            current_ts=decision.current_ts,
            current_ts_iso=decision.current_ts_iso,
            current_price=decision.trigger_price,
            line_price=decision.line_price,
            price_gap=decision.price_gap,
            price_gap_pct=decision.price_gap_pct,
            trend_direction=decision.trend_direction,
            action=decision.action,
            reason=decision.reason,
        )
        snapshots.append(snapshot)
        logger.info(
            "Watch check #%s: symbol=%s action=%s current_price=%s line_price=%s gap=%s gap_pct=%s reason=%s",
            snapshot.check_index,
            payload.symbol,
            snapshot.action,
            snapshot.current_price,
            snapshot.line_price,
            snapshot.price_gap,
            snapshot.price_gap_pct,
            snapshot.reason,
        )
        if on_snapshot is not None:
            on_snapshot(snapshot)

        if decision.action in ("BUY", "SELL"):
            breakout_action = decision.action
            logger.info(
                "Breakout detected: symbol=%s action=%s check_index=%s",
                payload.symbol,
                decision.action,
                snapshot.check_index,
            )
            if payload.mode == "live" and not live_order_submitted:
                place_live_order(payload, decision)
                live_order_submitted = True
            notify_breakout(payload, decision)
            if payload.stop_on_breakout:
                logger.info("Stopping watch because stop_on_breakout=true and breakout occurred")
                break

        i += 1
        if payload.max_checks is None or i < payload.max_checks:
            logger.debug("Sleeping before next check: interval_seconds=%s", payload.interval_seconds)
            time.sleep(payload.interval_seconds)

    ended_ts = int(time.time() * 1000)
    last_action: Literal["BUY", "SELL", "NONE"] = snapshots[-1].action if snapshots else "NONE"

    result = SignalWatchResult(
        symbol=payload.symbol,
        interval_seconds=payload.interval_seconds,
        max_checks=payload.max_checks,
        started_ts=started_ts,
        ended_ts=ended_ts,
        duration_seconds=(ended_ts - started_ts) / 1000.0,
        checks_run=len(snapshots),
        breakout_detected=breakout_action is not None,
        breakout_action=breakout_action,
        last_action=last_action,
        snapshots=snapshots,
    )
    logger.info(
        "Signal watch completed: symbol=%s checks_run=%s breakout_detected=%s breakout_action=%s duration_seconds=%s",
        result.symbol,
        result.checks_run,
        result.breakout_detected,
        result.breakout_action,
        result.duration_seconds,
    )
    return result


def notify_breakout(payload: SignalWatchRequest, decision: OrderDecision) -> None:
    notify_base_url = (
        payload.notify_url or os.getenv("BARK_NOTIFY_URL") or DEFAULT_BARK_NOTIFY_URL
    ).strip()
    if not notify_base_url:
        return

    # Bark supports /<key>/<title>/<body>. Keep content concise and URL-safe.
    title = quote(f"{decision.action} breakout {decision.symbol}", safe="")
    body_raw = (
        f"price={decision.trigger_price:.2f}, line={decision.line_price:.2f}, "
        f"gap={decision.price_gap:.2f} ({decision.price_gap_pct:.3f}%), ts={decision.current_ts_iso}"
    )
    body = quote(body_raw, safe="")
    url = f"{notify_base_url.rstrip('/')}/{title}/{body}"
    logger.info("Sending breakout notification: symbol=%s action=%s url=%s", decision.symbol, decision.action, url)

    req = Request(url, headers={"User-Agent": "trendline-api/0.1"})
    try:
        with urlopen(req, timeout=10):
            pass
        logger.info("Breakout notification sent successfully: symbol=%s action=%s", decision.symbol, decision.action)
    except Exception:
        # Notification is best-effort and must not break the watch job.
        logger.exception("Breakout notification failed: symbol=%s action=%s", decision.symbol, decision.action)
        return


def get_allowed_telegram_chat_ids() -> Optional[set[int]]:
    raw = str(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    if not raw:
        return None
    allowed: set[int] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            allowed.add(int(value))
        except ValueError:
            logger.warning("Ignoring invalid TELEGRAM_ALLOWED_CHAT_IDS entry: %s", value)
    return allowed or None


def is_allowed_telegram_chat(chat_id: int) -> bool:
    allowed = get_allowed_telegram_chat_ids()
    return True if allowed is None else chat_id in allowed


def get_chat_ai_inputs(chat_id: int) -> AiRecognitionInputs:
    with TELEGRAM_STATE_LOCK:
        current = TELEGRAM_CHAT_SETTINGS.get(chat_id)
        if current is not None:
            return current.model_copy(deep=True)
    return get_default_ai_inputs()


def set_chat_ai_inputs(chat_id: int, inputs: AiRecognitionInputs) -> None:
    with TELEGRAM_STATE_LOCK:
        TELEGRAM_CHAT_SETTINGS[chat_id] = inputs.model_copy(deep=True)


def reset_chat_ai_inputs(chat_id: int) -> AiRecognitionInputs:
    inputs = get_default_ai_inputs()
    with TELEGRAM_STATE_LOCK:
        TELEGRAM_CHAT_SETTINGS.pop(chat_id, None)
        TELEGRAM_LAST_RESULTS.pop(chat_id, None)
    return inputs


def set_chat_last_result(chat_id: int, result: dict[str, Any]) -> None:
    with TELEGRAM_STATE_LOCK:
        TELEGRAM_LAST_RESULTS[chat_id] = json.loads(json.dumps(result, ensure_ascii=False))


def get_chat_last_result(chat_id: int) -> Optional[dict[str, Any]]:
    with TELEGRAM_STATE_LOCK:
        result = TELEGRAM_LAST_RESULTS.get(chat_id)
        return None if result is None else json.loads(json.dumps(result, ensure_ascii=False))


def build_ai_config_summary(inputs: AiRecognitionInputs) -> str:
    return "\n".join(
        [
            f"symbol={inputs.symbol}",
            f"timeframe={inputs.timeframe}",
            f"usd_amount={inputs.usd_amount}",
            f"mode={inputs.mode}",
            f"chart_timezone={inputs.chart_timezone}",
            f"target_line_hint={inputs.target_line_hint or 'null'}",
        ]
    )


def build_telegram_help_text(inputs: AiRecognitionInputs) -> str:
    return "\n".join(
        [
            "发送一张 TradingView 截图给机器人，我会按 1.html 相同的 AI 识别逻辑返回趋势线 payload。",
            "",
            "建议把图片作为原图文件发送，清晰度更稳定。",
            "",
            "可在图片 caption 或 /config 中写参数，每行一个：",
            "symbol=BTCUSDT",
            "timeframe=1h",
            "usd_amount=100",
            "mode=simulate",
            "chart_timezone=UTC",
            "target_line_hint=蓝色下降压力线",
            "",
            "命令：",
            "/config 设置默认参数",
            "/showconfig 查看当前默认参数",
            "/resetconfig 恢复默认参数",
            "/last 查看最近一次识别结果",
            "",
            "当前默认参数：",
            build_ai_config_summary(inputs),
        ]
    )


def parse_ai_overrides(text: Any) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}

    if raw.startswith("/config"):
        raw = raw.partition(" ")[2].strip()
    if not raw:
        return {}

    alias_map = {
        "symbol": "symbol",
        "pair": "symbol",
        "timeframe": "timeframe",
        "interval": "timeframe",
        "usd": "usd_amount",
        "usd_amount": "usd_amount",
        "amount": "usd_amount",
        "mode": "mode",
        "timezone": "chart_timezone",
        "chart_timezone": "chart_timezone",
        "tz": "chart_timezone",
        "target_line_hint": "target_line_hint",
        "line_hint": "target_line_hint",
        "hint": "target_line_hint",
    }

    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("JSON 配置必须是对象")
        normalized: dict[str, Any] = {}
        for key, value in parsed.items():
            target = alias_map.get(str(key).strip().lower())
            if target:
                normalized[target] = value
        return normalized

    overrides: dict[str, Any] = {}
    free_text: list[str] = []
    for chunk in re.split(r"[\n;]+", raw):
        line = chunk.strip()
        if not line:
            continue
        match = re.match(r"([A-Za-z_][\w-]*)\s*[:=]\s*(.+)", line)
        if not match:
            free_text.append(line)
            continue
        key = alias_map.get(match.group(1).strip().lower())
        if not key:
            continue
        overrides[key] = match.group(2).strip()

    if free_text and "target_line_hint" not in overrides:
        overrides["target_line_hint"] = " ".join(free_text).strip()

    return overrides


def apply_ai_overrides(base: AiRecognitionInputs, overrides: dict[str, Any]) -> AiRecognitionInputs:
    merged = base.model_dump()
    merged.update(overrides or {})
    return AiRecognitionInputs(**merged)


def truncate_telegram_text(text: str, limit: int = 3800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 8].rstrip() + "\n[截断]"


def format_telegram_ai_result(result: dict[str, Any]) -> str:
    lines = [
        "AI 识别完成，可直接用于 /signal/watch 提交。" if result.get("ready_for_api") else "AI 识别完成，但当前未达到可提交状态。",
        f"confidence={float(result.get('confidence') or 0):.3f}",
        f"trendline_kind={result.get('trendline_kind') or 'unknown'}",
        f"symbol={result.get('symbol') or result.get('api_payload', {}).get('symbol') or 'null'}",
        f"timeframe={result.get('timeframe') or 'null'}",
        f"chart_timezone={result.get('chart_timezone') or 'null'}",
    ]
    recovery = result.get("recovery")
    if isinstance(recovery, dict) and recovery.get("mode"):
        lines.append(f"recovery={recovery['mode']}")
    if result.get("ready_for_api"):
        lines.extend(
            [
                "",
                "payload:",
                json.dumps(result.get("api_payload") or {}, ensure_ascii=False, indent=2),
            ]
        )
    if result.get("notes"):
        lines.extend(["", "notes:", str(result["notes"])])
    return truncate_telegram_text("\n".join(lines))


def telegram_api_call(token: str, method: str, payload: Optional[dict[str, Any]] = None, timeout: int = 30) -> Any:
    url = f"{TELEGRAM_API_BASE_URL}/bot{token}/{method}"
    response = request_json(url, method="POST", payload=payload or {}, timeout=timeout)
    if not isinstance(response, dict):
        raise RuntimeError(f"Telegram API {method} returned invalid response")
    if not response.get("ok"):
        raise RuntimeError(str(response.get("description") or f"Telegram API {method} failed"))
    return response.get("result")


def telegram_send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: Optional[int] = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": truncate_telegram_text(text),
        "disable_web_page_preview": True,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    telegram_api_call(token, "sendMessage", payload=payload, timeout=30)


def telegram_send_chat_action(token: str, chat_id: int, action: str) -> None:
    try:
        telegram_api_call(token, "sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)
    except Exception:
        logger.debug("Failed to send Telegram chat action", exc_info=True)


def guess_image_content_type(name: Optional[str], fallback: str = "image/jpeg") -> str:
    lower = str(name or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    return fallback


def extract_telegram_image_file(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best = max(photos, key=lambda item: int(item.get("file_size") or 0))
        if isinstance(best.get("file_id"), str):
            return {
                "file_id": best["file_id"],
                "content_type": "image/jpeg",
                "name": "photo.jpg",
            }

    document = message.get("document")
    if isinstance(document, dict):
        file_id = document.get("file_id")
        mime_type = str(document.get("mime_type") or "")
        file_name = str(document.get("file_name") or "image")
        lower_name = file_name.lower()
        is_image = mime_type.startswith("image/") or lower_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
        if isinstance(file_id, str) and is_image:
            return {
                "file_id": file_id,
                "content_type": mime_type or guess_image_content_type(file_name),
                "name": file_name,
            }

    return None


def download_telegram_file(token: str, file_id: str, *, preferred_content_type: str) -> tuple[bytes, str]:
    file_info = telegram_api_call(token, "getFile", {"file_id": file_id}, timeout=30)
    if not isinstance(file_info, dict) or not isinstance(file_info.get("file_path"), str):
        raise RuntimeError("Telegram getFile 返回缺少 file_path")
    file_url = f"{TELEGRAM_API_BASE_URL}/file/bot{token}/{file_info['file_path']}"
    data, detected_content_type = request_bytes(file_url, timeout=60)
    content_type = preferred_content_type or detected_content_type or "image/jpeg"
    if content_type == "application/octet-stream":
        content_type = guess_image_content_type(file_info["file_path"])
    return data, content_type


def handle_telegram_command(token: str, message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    text = str(message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower()
    current_inputs = get_chat_ai_inputs(chat_id)

    if command in {"/start", "/help"}:
        telegram_send_message(token, chat_id, build_telegram_help_text(current_inputs), reply_to_message_id=message_id)
        return

    if command == "/showconfig":
        telegram_send_message(
            token,
            chat_id,
            "当前默认参数：\n" + build_ai_config_summary(current_inputs),
            reply_to_message_id=message_id,
        )
        return

    if command == "/resetconfig":
        reset_inputs = reset_chat_ai_inputs(chat_id)
        telegram_send_message(
            token,
            chat_id,
            "已恢复默认参数：\n" + build_ai_config_summary(reset_inputs),
            reply_to_message_id=message_id,
        )
        return

    if command == "/last":
        last_result = get_chat_last_result(chat_id)
        if last_result is None:
            telegram_send_message(token, chat_id, "还没有最近一次识别结果。", reply_to_message_id=message_id)
            return
        telegram_send_message(token, chat_id, format_telegram_ai_result(last_result), reply_to_message_id=message_id)
        return

    if command == "/config":
        try:
            overrides = parse_ai_overrides(text)
            if not overrides:
                telegram_send_message(
                    token,
                    chat_id,
                    "当前默认参数：\n" + build_ai_config_summary(current_inputs),
                    reply_to_message_id=message_id,
                )
                return
            updated = apply_ai_overrides(current_inputs, overrides)
        except Exception as exc:
            telegram_send_message(token, chat_id, f"配置更新失败：{exc}", reply_to_message_id=message_id)
            return

        set_chat_ai_inputs(chat_id, updated)
        telegram_send_message(
            token,
            chat_id,
            "默认参数已更新：\n" + build_ai_config_summary(updated),
            reply_to_message_id=message_id,
        )
        return

    telegram_send_message(token, chat_id, "未知命令，发送 /help 查看用法。", reply_to_message_id=message_id)


def handle_telegram_image_message(token: str, message: dict[str, Any], image_file: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    base_inputs = get_chat_ai_inputs(chat_id)

    try:
        overrides = parse_ai_overrides(message.get("caption"))
        inputs = apply_ai_overrides(base_inputs, overrides) if overrides else base_inputs
    except Exception as exc:
        telegram_send_message(token, chat_id, f"参数解析失败：{exc}", reply_to_message_id=message_id)
        return

    telegram_send_chat_action(token, chat_id, "typing")
    telegram_send_message(token, chat_id, "已收到截图，开始识别。", reply_to_message_id=message_id)

    try:
        image_bytes, content_type = download_telegram_file(
            token,
            str(image_file["file_id"]),
            preferred_content_type=str(image_file.get("content_type") or "image/jpeg"),
        )
        result = recognize_trendline_from_image(image_bytes, inputs, image_content_type=content_type)
        set_chat_last_result(chat_id, result)
        telegram_send_message(token, chat_id, format_telegram_ai_result(result), reply_to_message_id=message_id)
    except Exception as exc:
        logger.exception("Telegram image recognition failed: chat_id=%s", chat_id)
        telegram_send_message(token, chat_id, f"识别失败：{exc}", reply_to_message_id=message_id)


def handle_telegram_message(token: str, update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return
    chat_id = int(chat["id"])
    message_id = int(message.get("message_id") or 0)

    if not is_allowed_telegram_chat(chat_id):
        telegram_send_message(token, chat_id, "当前 chat_id 未被允许使用该机器人。", reply_to_message_id=message_id or None)
        return

    text = str(message.get("text") or "").strip()
    if text.startswith("/"):
        handle_telegram_command(token, message)
        return

    image_file = extract_telegram_image_file(message)
    if image_file is not None:
        worker = threading.Thread(
            target=handle_telegram_image_message,
            args=(token, message, image_file),
            daemon=True,
            name=f"telegram-ai-{chat_id}",
        )
        worker.start()
        return

    if text:
        telegram_send_message(token, chat_id, "请发送截图或使用 /help 查看用法。", reply_to_message_id=message_id or None)


def telegram_bot_loop(token: str) -> None:
    logger.info("Telegram bot polling loop started")
    try:
        telegram_api_call(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=30)
    except Exception:
        logger.exception("Failed to delete Telegram webhook before polling")

    offset: Optional[int] = None
    poll_timeout = int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS") or "60")
    while not TELEGRAM_BOT_STOP_EVENT.is_set():
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset

        try:
            updates = telegram_api_call(token, "getUpdates", payload=payload, timeout=poll_timeout + 15)
        except Exception:
            logger.exception("Telegram getUpdates failed")
            time.sleep(5)
            continue

        if not isinstance(updates, list):
            continue

        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            try:
                handle_telegram_message(token, update)
            except Exception:
                logger.exception("Telegram update handler crashed: update_id=%s", update_id)

    logger.info("Telegram bot polling loop stopped")


def maybe_start_telegram_bot() -> None:
    global TELEGRAM_BOT_THREAD

    token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.info("Telegram bot disabled: TELEGRAM_BOT_TOKEN not configured")
        return

    openrouter_key = str(os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not openrouter_key:
        logger.warning("Telegram bot disabled: OPENROUTER_API_KEY not configured")
        return

    if TELEGRAM_BOT_THREAD is not None and TELEGRAM_BOT_THREAD.is_alive():
        return

    TELEGRAM_BOT_STOP_EVENT.clear()
    TELEGRAM_BOT_THREAD = threading.Thread(
        target=telegram_bot_loop,
        args=(token,),
        daemon=True,
        name="telegram-bot-polling",
    )
    TELEGRAM_BOT_THREAD.start()
    logger.info("Telegram bot polling thread started")


@app.on_event("startup")
def on_startup() -> None:
    maybe_start_telegram_bot()


@app.on_event("shutdown")
def on_shutdown() -> None:
    TELEGRAM_BOT_STOP_EVENT.set()


def set_job_running(job_id: str) -> Optional[SignalWatchRequest]:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return None
        job.status = "running"
        job.started_ts = int(time.time() * 1000)
        logger.info("Job state updated: job_id=%s status=running", job_id)
        return job.payload.model_copy(deep=True)


def update_job_snapshot(job_id: str, snapshot: SignalCheckSnapshot) -> None:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return
        job.checks_run = snapshot.check_index
        job.last_snapshot = snapshot
        logger.debug(
            "Job snapshot updated: job_id=%s checks_run=%s action=%s",
            job_id,
            snapshot.check_index,
            snapshot.action,
        )


def set_job_completed(job_id: str, result: SignalWatchResult) -> None:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return
        job.status = "completed"
        job.ended_ts = int(time.time() * 1000)
        job.result = result
        job.checks_run = result.checks_run
        job.last_snapshot = result.snapshots[-1] if result.snapshots else None
        logger.info("Job state updated: job_id=%s status=completed checks_run=%s", job_id, result.checks_run)


def set_job_failed(job_id: str, error: str) -> None:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return
        job.status = "failed"
        job.ended_ts = int(time.time() * 1000)
        job.error = error
        logger.error("Job state updated: job_id=%s status=failed error=%s", job_id, error)


def run_watch_job(job_id: str) -> None:
    logger.info("Background watch job started: job_id=%s", job_id)
    payload = set_job_running(job_id)
    if payload is None:
        logger.warning("Background watch job skipped: job_id=%s not found", job_id)
        return
    try:
        result = watch_signal(payload, on_snapshot=lambda s: update_job_snapshot(job_id, s))
        set_job_completed(job_id, result)
    except Exception as e:
        logger.exception("Background watch job crashed: job_id=%s", job_id)
        set_job_failed(job_id, str(e))


@app.get("/healthz", include_in_schema=False)
@app.head("/healthz", include_in_schema=False)
def healthz() -> Response:
    return Response(status_code=200)


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
@app.get("/manual", include_in_schema=False)
@app.head("/manual", include_in_schema=False)
@app.get("/ai", include_in_schema=False)
@app.head("/ai", include_in_schema=False)
def serve_ui():
    if not WEB_ENTRY.exists():
        raise HTTPException(status_code=404, detail="1.html not found")
    return FileResponse(WEB_ENTRY)


@app.post("/ai/recognize")
def ai_recognize(payload: AiRecognitionRequest):
    try:
        image_bytes, image_content_type = decode_image_data_url(payload.image_data_url)
        inputs = AiRecognitionInputs(**payload.model_dump(exclude={"image_data_url"}))
        return recognize_trendline_from_image(
            image_bytes,
            inputs,
            image_content_type=image_content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 500 if "OPENROUTER_API_KEY 未配置" in detail else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/signal/watch", response_model=SignalWatchJobAccepted)
def signal_watch(payload: SignalWatchRequest):
    if payload.max_checks is None and not payload.stop_on_breakout:
        raise HTTPException(
            status_code=400,
            detail="For unlimited monitoring, stop_on_breakout must be true",
        )
    job_id = uuid.uuid4().hex
    created_ts = int(time.time() * 1000)
    with WATCH_JOBS_LOCK:
        WATCH_JOBS[job_id] = WatchJobState(
            job_id=job_id,
            payload=payload.model_copy(deep=True),
            status="queued",
            created_ts=created_ts,
        )

    thread = threading.Thread(target=run_watch_job, args=(job_id,), daemon=True)
    thread.start()
    logger.info(
        "Watch job queued: job_id=%s symbol=%s interval_seconds=%s max_checks=%s stop_on_breakout=%s",
        job_id,
        payload.symbol,
        payload.interval_seconds,
        payload.max_checks,
        payload.stop_on_breakout,
    )

    return SignalWatchJobAccepted(job_id=job_id, status="queued", created_ts=created_ts)


@app.get("/signal/watch/{job_id}", response_model=SignalWatchJobStatus)
def signal_watch_status(job_id: str):
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            logger.warning("Watch job status requested but not found: job_id=%s", job_id)
            raise HTTPException(status_code=404, detail="job not found")
        logger.debug("Watch job status requested: job_id=%s status=%s", job_id, job.status)

        return SignalWatchJobStatus(
            job_id=job.job_id,
            status=job.status,
            symbol=job.payload.symbol,
            interval_seconds=job.payload.interval_seconds,
            max_checks=job.payload.max_checks,
            stop_on_breakout=job.payload.stop_on_breakout,
            created_ts=job.created_ts,
            started_ts=job.started_ts,
            ended_ts=job.ended_ts,
            checks_run=job.checks_run,
            last_snapshot=job.last_snapshot,
            error=job.error,
            result=job.result,
        )
