from __future__ import annotations

import json
import hashlib
import hmac
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator


app = FastAPI(title="Trendline Breakout API", version="0.1.0")


DEFAULT_BARK_NOTIFY_URL = "https://api.day.app/j32eBocVfwx6kvf8xr452K/"
DEFAULT_BINANCE_BASE_URL = "https://api.binance.us"
LOG_LEVEL = str(os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trendline_api")
ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST_DIR = ROOT_DIR / "frontend" / "dist"
STRATEGY_WEB_ENTRY = FRONTEND_DIST_DIR / "index.html"
SETTINGS_WEB_ENTRY = ROOT_DIR / "settings.html"
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

DEFAULT_CHART_TIMEZONE = "Asia/Shanghai"


def normalize_chart_timezone(value: str) -> str:
    """Accept common UTC+8 labels while storing a stable IANA timezone."""
    normalized = (value or "").strip() or DEFAULT_CHART_TIMEZONE
    aliases = {
        "UTC+8": DEFAULT_CHART_TIMEZONE,
        "UTC+08:00": DEFAULT_CHART_TIMEZONE,
        "GMT+8": DEFAULT_CHART_TIMEZONE,
        "GMT+08:00": DEFAULT_CHART_TIMEZONE,
    }
    normalized = aliases.get(normalized.upper(), normalized)
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid chart_timezone: {value}") from exc
    return normalized
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
if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="frontend-assets")


JOB_STATUS = Literal["queued", "running", "completed", "failed", "cancelled"]


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
    owner_chat_id: Optional[int] = None
    started_ts: Optional[int] = None
    ended_ts: Optional[int] = None
    checks_run: int = 0
    last_snapshot: Optional[SignalCheckSnapshot] = None
    error: Optional[str] = None
    result: Optional[SignalWatchResult] = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class WatchJobCancelSummary:
    queued_job_ids: list[str]
    running_job_ids: list[str]


class WatchJobCancelled(Exception):
    pass


WATCH_JOBS: dict[str, WatchJobState] = {}
WATCH_JOBS_LOCK = threading.Lock()


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
    cancel_event: Optional[threading.Event] = None,
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
        ensure_watch_not_cancelled(cancel_event, reason="watch job cancelled by user")
        if payload.max_checks is not None and i >= payload.max_checks:
            break

        check_payload = payload.model_copy(update={"current_ts": None, "current_price": None})
        decision = decide_order(check_payload)
        ensure_watch_not_cancelled(cancel_event, reason="watch job cancelled by user")
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

        ensure_watch_not_cancelled(cancel_event, reason="watch job cancelled by user")
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
            if cancel_event is not None and cancel_event.wait(payload.interval_seconds):
                raise WatchJobCancelled("watch job cancelled by user")
            if cancel_event is None:
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


def set_job_running(job_id: str) -> Optional[tuple[SignalWatchRequest, threading.Event]]:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return None
        if job.status == "cancelled" or job.cancel_event.is_set():
            if job.status != "cancelled":
                job.status = "cancelled"
                job.ended_ts = int(time.time() * 1000)
                job.error = job.error or "watch job cancelled before start"
            logger.info("Job start skipped because it was already cancelled: job_id=%s", job_id)
            return None
        if job.status != "queued":
            logger.warning("Job start skipped because status is unexpected: job_id=%s status=%s", job_id, job.status)
            return None
        job.status = "running"
        job.started_ts = int(time.time() * 1000)
        logger.info("Job state updated: job_id=%s status=running", job_id)
        return job.payload.model_copy(deep=True), job.cancel_event


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


def set_job_cancelled(job_id: str, reason: str) -> None:
    with WATCH_JOBS_LOCK:
        job = WATCH_JOBS.get(job_id)
        if job is None:
            return
        job.status = "cancelled"
        job.ended_ts = int(time.time() * 1000)
        job.error = reason
        logger.info("Job state updated: job_id=%s status=cancelled reason=%s", job_id, reason)


def ensure_watch_not_cancelled(cancel_event: Optional[threading.Event], *, reason: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise WatchJobCancelled(reason)


def run_watch_job(job_id: str) -> None:
    logger.info("Background watch job started: job_id=%s", job_id)
    execution = set_job_running(job_id)
    if execution is None:
        logger.info("Background watch job skipped: job_id=%s", job_id)
        return
    payload, cancel_event = execution
    try:
        result = watch_signal(
            payload,
            on_snapshot=lambda s: update_job_snapshot(job_id, s),
            cancel_event=cancel_event,
        )
        set_job_completed(job_id, result)
    except WatchJobCancelled as exc:
        set_job_cancelled(job_id, str(exc))
    except Exception as e:
        logger.exception("Background watch job crashed: job_id=%s", job_id)
        set_job_failed(job_id, str(e))


def validate_signal_watch_request(payload: SignalWatchRequest) -> None:
    if payload.max_checks is None and not payload.stop_on_breakout:
        raise ValueError("For unlimited monitoring, stop_on_breakout must be true")


def enqueue_signal_watch(
    payload: SignalWatchRequest,
    *,
    owner_chat_id: Optional[int] = None,
) -> SignalWatchJobAccepted:
    validate_signal_watch_request(payload)
    job_id = uuid.uuid4().hex
    created_ts = int(time.time() * 1000)
    with WATCH_JOBS_LOCK:
        WATCH_JOBS[job_id] = WatchJobState(
            job_id=job_id,
            payload=payload.model_copy(deep=True),
            status="queued",
            created_ts=created_ts,
            owner_chat_id=owner_chat_id,
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


@app.get("/healthz", include_in_schema=False)
@app.head("/healthz", include_in_schema=False)
def healthz() -> Response:
    return Response(status_code=200)


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
@app.get("/strategy", include_in_schema=False)
@app.head("/strategy", include_in_schema=False)
def serve_strategy_ui():
    if not STRATEGY_WEB_ENTRY.exists():
        raise HTTPException(status_code=404, detail="Vue frontend has not been built")
    return FileResponse(STRATEGY_WEB_ENTRY)


@app.get("/settings", include_in_schema=False)
@app.head("/settings", include_in_schema=False)
def serve_settings_ui():
    if not SETTINGS_WEB_ENTRY.exists():
        raise HTTPException(status_code=404, detail="settings.html not found")
    return FileResponse(SETTINGS_WEB_ENTRY)


@app.post("/signal/watch", response_model=SignalWatchJobAccepted)
def signal_watch(payload: SignalWatchRequest):
    try:
        return enqueue_signal_watch(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


# ---------------------------------------------------------------------------
# Two-image futures strategy builder
# ---------------------------------------------------------------------------

FUTURES_STRATEGY_STATUSES = {
    "armed",
    "entering",
    "position_open",
    "exiting",
    "completed",
    "cancelled",
    "position_left_open",
    "failed",
    "attention_required",
}
ACTIVE_FUTURES_STRATEGY_STATUSES = {"armed", "entering", "position_open", "exiting"}


class LineSpec(BaseModel):
    kind: Literal["horizontal", "trendline"]
    price: Optional[float] = Field(None, gt=0)
    ts1: Optional[int] = None
    price1: Optional[float] = Field(None, gt=0)
    ts2: Optional[int] = None
    price2: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "horizontal":
            if self.price is None:
                raise ValueError("horizontal line requires price")
            return self
        required = (self.ts1, self.price1, self.ts2, self.price2)
        if any(value is None for value in required):
            raise ValueError("trendline requires ts1, price1, ts2 and price2")
        if self.ts1 == self.ts2:
            raise ValueError("trendline ts1 and ts2 must differ")
        return self

    def price_at(self, ts_ms: int) -> float:
        if self.kind == "horizontal":
            return float(self.price)
        return float(self.price1) + (float(self.price2) - float(self.price1)) * (
            (ts_ms - int(self.ts1)) / (int(self.ts2) - int(self.ts1))
        )


class FuturesStrategyRequest(BaseModel):
    symbol: str = Field("BTCUSDT", min_length=3)
    timeframe: str = Field("1h", min_length=1)
    chart_timezone: str = Field(DEFAULT_CHART_TIMEZONE, min_length=1)
    direction: Literal["LONG", "SHORT"]
    notional_usdt: float = Field(..., gt=0)
    leverage: int = Field(30, ge=1, le=125)
    mode: Literal["simulate", "live"] = "simulate"
    entry_line: LineSpec
    stop_line: Optional[LineSpec] = None

    @field_validator("symbol")
    @classmethod
    def normalize_strategy_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("timeframe")
    @classmethod
    def normalize_strategy_timeframe(cls, value: str) -> str:
        value = value.strip()
        if value not in INTERVAL_TO_MS:
            raise ValueError("unsupported timeframe")
        return value

    @field_validator("chart_timezone")
    @classmethod
    def validate_strategy_timezone(cls, value: str) -> str:
        return normalize_chart_timezone(value)


class BarkSettingsUpdate(BaseModel):
    endpoint: str = ""
    enabled: bool = False
    notify_on_open: bool = True
    notify_on_close: bool = True

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Bark 地址必须是完整的 http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("Bark 地址不能包含用户名或密码")
        return value.rstrip("/") + "/"


@dataclass
class FuturesStrategyState:
    strategy_id: str
    payload: FuturesStrategyRequest
    status: str
    created_ts: int
    updated_ts: int
    current_price: Optional[float] = None
    current_price_ts: Optional[int] = None
    entry_line_price: Optional[float] = None
    stop_line_price: Optional[float] = None
    feed_state: str = "connecting"
    filled_qty: Optional[float] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    entry_order: Optional[dict[str, Any]] = None
    exit_order: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    close_on_cancel: Optional[bool] = None
    restored: bool = False
    last_persist_monotonic: float = field(default=0.0, repr=False)


FUTURES_STRATEGIES: dict[str, FuturesStrategyState] = {}
FUTURES_STRATEGIES_LOCK = threading.Lock()
FUTURES_DB_LOCK = threading.Lock()


def get_futures_base_url() -> str:
    return str(os.getenv("BINANCE_FUTURES_BASE_URL") or "https://fapi.binance.com").rstrip("/")


def get_futures_ws_url(symbol: str) -> str:
    configured = str(os.getenv("BINANCE_FUTURES_WS_URL") or "wss://fstream.binance.com/ws").rstrip("/")
    return f"{configured}/{symbol.lower()}@aggTrade"


def get_strategy_db_path() -> Path:
    raw = str(os.getenv("STRATEGY_DB_PATH") or (ROOT_DIR / "strategy_state.db"))
    return Path(raw).expanduser().resolve()


def init_strategy_db() -> None:
    path = get_strategy_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with FUTURES_DB_LOCK, sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_strategies (
                strategy_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_ts INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def default_bark_settings() -> BarkSettingsUpdate:
    endpoint = str(os.getenv("BARK_NOTIFY_URL") or "").strip()
    return BarkSettingsUpdate(
        endpoint=endpoint,
        enabled=bool(endpoint),
        notify_on_open=True,
        notify_on_close=True,
    )


def get_bark_settings() -> BarkSettingsUpdate:
    init_strategy_db()
    with FUTURES_DB_LOCK, sqlite3.connect(get_strategy_db_path()) as conn:
        row = conn.execute(
            "SELECT value_json FROM app_settings WHERE setting_key = ?",
            ("bark",),
        ).fetchone()
    if row is None:
        return default_bark_settings()
    try:
        return BarkSettingsUpdate.model_validate_json(row[0])
    except Exception:
        logger.exception("Invalid persisted Bark settings; using environment defaults")
        return default_bark_settings()


def save_bark_settings(settings: BarkSettingsUpdate) -> BarkSettingsUpdate:
    if settings.enabled and not settings.endpoint:
        raise ValueError("启用 Bark 通知前请填写 Bark 地址")
    init_strategy_db()
    with FUTURES_DB_LOCK, sqlite3.connect(get_strategy_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(setting_key, value_json, updated_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
              value_json=excluded.value_json,
              updated_ts=excluded.updated_ts
            """,
            ("bark", settings.model_dump_json(), int(time.time() * 1000)),
        )
        conn.commit()
    return settings


def send_bark_message(endpoint: str, title: str, body: str) -> None:
    url = f"{endpoint.rstrip('/')}/{quote(title, safe='')}/{quote(body, safe='')}"
    req = Request(url, method="GET", headers={"User-Agent": "btc-breakout/0.2"})
    with urlopen(req, timeout=10):
        return


def notify_strategy_bark(state: FuturesStrategyState, event: Literal["open", "close"]) -> None:
    try:
        settings = get_bark_settings()
        should_send = settings.enabled and (
            (event == "open" and settings.notify_on_open)
            or (event == "close" and settings.notify_on_close)
        )
        if not should_send or not settings.endpoint:
            return
        mode = "实盘" if state.payload.mode == "live" else "模拟"
        side = "做多" if state.payload.direction == "LONG" else "做空"
        if event == "open":
            title = f"{state.payload.symbol} 已开仓"
            body = f"{mode} {side} · {state.payload.leverage}x · 成交价 {state.entry_price:g}"
        else:
            title = f"{state.payload.symbol} 已平仓"
            body = f"{mode} {side} · 平仓价 {state.exit_price:g}"
        send_bark_message(settings.endpoint, title, body)
        add_strategy_event(state, "bark_notified", f"Bark {event} 通知已发送")
    except Exception as exc:
        logger.warning("Bark strategy notification failed: strategy_id=%s event=%s error=%s", state.strategy_id, event, exc)
        add_strategy_event(state, "bark_notify_failed", f"Bark 通知发送失败：{exc}", event=event)


def strategy_public_dict(state: FuturesStrategyState) -> dict[str, Any]:
    return {
        "strategy_id": state.strategy_id,
        "status": state.status,
        "created_ts": state.created_ts,
        "updated_ts": state.updated_ts,
        "symbol": state.payload.symbol,
        "timeframe": state.payload.timeframe,
        "direction": state.payload.direction,
        "notional_usdt": state.payload.notional_usdt,
        "leverage": state.payload.leverage,
        "margin_type": "CROSSED",
        "mode": state.payload.mode,
        "entry_line": state.payload.entry_line.model_dump(),
        "stop_line": state.payload.stop_line.model_dump() if state.payload.stop_line else None,
        "current_price": state.current_price,
        "current_price_ts": state.current_price_ts,
        "entry_line_price": state.entry_line_price,
        "stop_line_price": state.stop_line_price,
        "feed_state": state.feed_state,
        "filled_qty": state.filled_qty,
        "entry_price": state.entry_price,
        "exit_price": state.exit_price,
        "entry_order": state.entry_order,
        "exit_order": state.exit_order,
        "error": state.error,
        "events": state.events[-100:],
    }


def persist_strategy(state: FuturesStrategyState) -> None:
    init_strategy_db()
    snapshot = strategy_public_dict(state)
    with FUTURES_DB_LOCK, sqlite3.connect(get_strategy_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO futures_strategies(strategy_id, payload_json, state_json, status, updated_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
              payload_json=excluded.payload_json,
              state_json=excluded.state_json,
              status=excluded.status,
              updated_ts=excluded.updated_ts
            """,
            (
                state.strategy_id,
                state.payload.model_dump_json(),
                json.dumps(snapshot, ensure_ascii=False),
                state.status,
                state.updated_ts,
            ),
        )
        conn.commit()


def add_strategy_event(state: FuturesStrategyState, event_type: str, message: str, **details: Any) -> None:
    now = int(time.time() * 1000)
    state.updated_ts = now
    state.events.append({"ts": now, "type": event_type, "message": message, "details": details})
    state.events = state.events[-200:]
    persist_strategy(state)


def update_strategy_status(state: FuturesStrategyState, status: str, message: str, **details: Any) -> None:
    if status not in FUTURES_STRATEGY_STATUSES:
        raise ValueError(f"invalid strategy status: {status}")
    state.status = status
    add_strategy_event(state, "status", message, status=status, **details)


def futures_public_json(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    url = f"{get_futures_base_url()}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return request_json(url, timeout=10)


def futures_signed_json(path: str, *, method: str = "GET", params: Optional[dict[str, Any]] = None) -> Any:
    api_key = str(os.getenv("BINANCE_API_KEY") or "").strip()
    api_secret = str(os.getenv("BINANCE_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("live mode requires BINANCE_API_KEY and BINANCE_API_SECRET")
    signed = {str(k): v for k, v in (params or {}).items() if v is not None}
    signed["timestamp"] = int(time.time() * 1000)
    signed["recvWindow"] = 5000
    query = urlencode(signed)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    body = f"{query}&signature={signature}".encode()
    url = f"{get_futures_base_url()}{path}"
    if method == "GET":
        url = f"{url}?{body.decode()}"
        data = None
    else:
        data = body
    req = Request(
        url,
        data=data,
        method=method,
        headers={
            "X-MBX-APIKEY": api_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "trendline-api/0.2",
        },
    )
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Binance Futures HTTP {exc.code}: {detail or exc.reason}") from exc


def fetch_futures_price(symbol: str) -> float:
    data = futures_public_json("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])


def fetch_spot_reference_price(symbol: str) -> float:
    data = request_json(
        "https://data-api.binance.vision/api/v3/ticker/price?" + urlencode({"symbol": symbol}),
        timeout=10,
    )
    return float(data["price"])


def fetch_futures_klines(
    symbol: str,
    timeframe: str,
    limit: int = 500,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
) -> list[dict[str, Any]]:
    params = {"symbol": symbol, "interval": timeframe, "limit": max(10, min(limit, 500))}
    if start_time is not None:
        params["startTime"] = int(start_time)
    if end_time is not None:
        params["endTime"] = int(end_time)
    configured = get_futures_base_url()
    bases = [configured]
    if configured == "https://fapi.binance.com":
        bases.extend(["https://fapi1.binance.com", "https://fapi2.binance.com", "https://fapi3.binance.com"])
    errors: list[str] = []
    raw: Any = None
    for base in bases:
        try:
            raw = request_json(f"{base}/fapi/v1/klines?{urlencode(params)}", timeout=10)
            if not isinstance(raw, list) or not raw:
                raise RuntimeError("Binance 返回了空 K 线数据")
            break
        except Exception as exc:
            errors.append(f"{urlsplit(base).netloc}: {exc}")
    source = "binance_futures"
    if not isinstance(raw, list) or not raw:
        try:
            fallback_url = "https://data-api.binance.vision/api/v3/klines?" + urlencode(params)
            raw = request_json(fallback_url, timeout=10)
            if not isinstance(raw, list) or not raw:
                raise RuntimeError("Binance 参考 K 线返回空数据")
            source = "binance_spot_fallback"
            logger.warning("Futures K lines unavailable; using Binance spot reference candles for preview: %s", "；".join(errors))
        except Exception as exc:
            errors.append(f"data-api.binance.vision: {exc}")
            raise RuntimeError("K 线源均不可用；" + "；".join(errors)) from exc
    return [
        {"ts": int(item[0]), "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4]), "source": source}
        for item in raw
    ]


def validate_stop_side(
    payload: FuturesStrategyRequest, current_price: float, ts_ms: int
) -> tuple[float, Optional[float]]:
    entry_price = payload.entry_line.price_at(ts_ms)
    if payload.stop_line is None:
        return entry_price, None
    stop_price = payload.stop_line.price_at(ts_ms)
    if payload.direction == "LONG" and stop_price >= current_price:
        raise ValueError("LONG 止损线必须低于当前价格")
    if payload.direction == "SHORT" and stop_price <= current_price:
        raise ValueError("SHORT 止损线必须高于当前价格")
    return entry_price, stop_price


def live_strategy_preflight(payload: FuturesStrategyRequest, *, configure: bool = False) -> None:
    if str(os.getenv("ENABLE_LIVE_FUTURES") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise ValueError("live futures 未启用；请设置 ENABLE_LIVE_FUTURES=true")
    position_mode = futures_signed_json("/fapi/v1/positionSide/dual")
    if bool(position_mode.get("dualSidePosition")):
        raise ValueError("live v1 仅支持 Binance One-way Mode")
    positions = futures_signed_json("/fapi/v2/positionRisk", params={"symbol": payload.symbol})
    if any(abs(float(item.get("positionAmt") or 0)) > 0 for item in positions if isinstance(item, dict)):
        raise ValueError(f"{payload.symbol} 已有持仓，不能启用独占策略")
    orders = futures_signed_json("/fapi/v1/openOrders", params={"symbol": payload.symbol})
    if orders:
        raise ValueError(f"{payload.symbol} 存在未完成订单，不能启用独占策略")
    if configure:
        try:
            futures_signed_json("/fapi/v1/marginType", method="POST", params={"symbol": payload.symbol, "marginType": "CROSSED"})
        except RuntimeError as exc:
            if "-4046" not in str(exc):
                raise
        futures_signed_json("/fapi/v1/leverage", method="POST", params={"symbol": payload.symbol, "leverage": payload.leverage})


def get_symbol_quantity_rules(symbol: str) -> tuple[float, float, float]:
    info = futures_public_json("/fapi/v1/exchangeInfo")
    item = next((row for row in info.get("symbols", []) if row.get("symbol") == symbol), None)
    if not item:
        raise ValueError(f"unknown futures symbol: {symbol}")
    filters = {entry.get("filterType"): entry for entry in item.get("filters", [])}
    lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
    notional = filters.get("MIN_NOTIONAL") or {}
    return float(lot.get("stepSize") or 0.001), float(lot.get("minQty") or 0.001), float(notional.get("notional") or 5)


def normalize_futures_quantity(symbol: str, notional_usdt: float, price: float) -> float:
    step, min_qty, min_notional = get_symbol_quantity_rules(symbol)
    raw = notional_usdt / price
    qty = math.floor((raw + 1e-12) / step) * step
    decimals = max(0, len(f"{step:.12f}".rstrip("0").partition(".")[2]))
    qty = round(qty, decimals)
    if qty < min_qty or qty * price < min_notional:
        raise ValueError("名义仓位低于 Binance 最小下单限制")
    return qty


def get_live_position_amount(symbol: str) -> float:
    positions = futures_signed_json("/fapi/v2/positionRisk", params={"symbol": symbol})
    if isinstance(positions, dict):
        positions = [positions]
    item = next((row for row in positions if isinstance(row, dict) and row.get("symbol") == symbol), None)
    return float((item or {}).get("positionAmt") or 0)


def reconcile_restored_live_position(state: FuturesStrategyState) -> None:
    amount = get_live_position_amount(state.payload.symbol)
    expected_sign = 1 if state.payload.direction == "LONG" else -1
    if amount == 0 or (amount > 0) != (expected_sign > 0):
        raise RuntimeError("重启恢复时 Binance 实际持仓与策略方向不一致，请人工核对")
    actual_qty = abs(amount)
    if state.filled_qty and actual_qty + 1e-12 < state.filled_qty:
        add_strategy_event(
            state,
            "position_reconciled",
            "Binance 实际持仓小于本地记录，已按实际数量继续保护",
            recorded_quantity=state.filled_qty,
            actual_quantity=actual_qty,
        )
    state.filled_qty = actual_qty


def submit_futures_market_order(state: FuturesStrategyState, *, closing: bool, reference_price: float) -> dict[str, Any]:
    payload = state.payload
    side = ("SELL" if payload.direction == "LONG" else "BUY") if closing else ("BUY" if payload.direction == "LONG" else "SELL")
    if closing:
        live_amount = get_live_position_amount(payload.symbol)
        expected_positive = payload.direction == "LONG"
        if live_amount == 0 or (live_amount > 0) != expected_positive:
            raise RuntimeError("Binance 实际持仓为空或方向不一致，未提交自动平仓单")
        qty = min(float(state.filled_qty or abs(live_amount)), abs(live_amount))
    else:
        qty = normalize_futures_quantity(payload.symbol, payload.notional_usdt, reference_price)
    if not qty:
        raise RuntimeError("futures order quantity is empty")
    suffix = "exit" if closing else "entry"
    client_id = f"btcbo-{state.strategy_id[:20]}-{suffix}"[:36]
    params: dict[str, Any] = {
        "symbol": payload.symbol,
        "side": side,
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": _format_order_qty(float(qty)),
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }
    if closing:
        params["reduceOnly"] = "true"
    try:
        return futures_signed_json("/fapi/v1/order", method="POST", params=params)
    except RuntimeError:
        try:
            existing = futures_signed_json(
                "/fapi/v1/order",
                params={"symbol": payload.symbol, "origClientOrderId": client_id},
            )
        except Exception:
            raise
        if existing and existing.get("orderId"):
            return existing
        raise


def order_fill_values(order: dict[str, Any], fallback_price: float, fallback_qty: float) -> tuple[float, float]:
    qty = to_finite_number(order.get("executedQty")) or fallback_qty
    price = to_finite_number(order.get("avgPrice")) or fallback_price
    return float(qty), float(price)


def strategy_price_stream(state: FuturesStrategyState):
    stale_since: Optional[float] = None
    while not state.cancel_event.is_set():
        ws = None
        try:
            import websocket  # type: ignore

            ws = websocket.create_connection(get_futures_ws_url(state.payload.symbol), timeout=3)
            state.feed_state = "live"
            stale_since = None
            while not state.cancel_event.is_set():
                event = json.loads(ws.recv())
                price = to_finite_number(event.get("p"))
                event_ts = to_int(event.get("T")) or int(time.time() * 1000)
                if price and price > 0:
                    yield float(price), event_ts, "live"
        except Exception as exc:
            logger.warning("Futures websocket unavailable for %s: %s", state.payload.symbol, exc)
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            if stale_since is None:
                stale_since = time.monotonic()
            try:
                price = fetch_futures_price(state.payload.symbol)
                stale_since = None
                yield price, int(time.time() * 1000), "degraded"
            except Exception as rest_exc:
                if state.payload.mode == "simulate":
                    try:
                        price = fetch_spot_reference_price(state.payload.symbol)
                        stale_since = None
                        yield price, int(time.time() * 1000), "reference"
                    except Exception as spot_exc:
                        state.feed_state = "stale"
                        state.error = f"行情暂时不可用: {spot_exc}"
                else:
                    state.feed_state = "stale"
                    state.error = f"行情暂时不可用: {rest_exc}"
                if stale_since and time.monotonic() - stale_since >= 5:
                    add_strategy_event(state, "market_data_stale", "行情超过 5 秒未更新", error=state.error)
            state.cancel_event.wait(1)


def should_enter(payload: FuturesStrategyRequest, current_price: float, line_price: float) -> bool:
    return current_price >= line_price if payload.direction == "LONG" else current_price <= line_price


def should_stop(payload: FuturesStrategyRequest, current_price: float, line_price: float) -> bool:
    return current_price <= line_price if payload.direction == "LONG" else current_price >= line_price


def close_strategy_position(state: FuturesStrategyState, current_price: float, *, cancelled: bool = False) -> None:
    update_strategy_status(state, "exiting", "正在提交 reduce-only 市价平仓")
    if state.payload.mode == "live":
        order = submit_futures_market_order(state, closing=True, reference_price=current_price)
        state.exit_order = order
        _, state.exit_price = order_fill_values(order, current_price, float(state.filled_qty or 0))
    else:
        state.exit_price = current_price
        state.exit_order = {"simulated": True, "side": "SELL" if state.payload.direction == "LONG" else "BUY"}
    update_strategy_status(state, "cancelled" if cancelled else "completed", "仓位已平仓，策略结束", exit_price=state.exit_price)
    notify_strategy_bark(state, "close")


def run_futures_strategy(strategy_id: str) -> None:
    with FUTURES_STRATEGIES_LOCK:
        state = FUTURES_STRATEGIES.get(strategy_id)
    if state is None:
        return
    try:
        if state.status in {"entering", "exiting"}:
            update_strategy_status(state, "attention_required", "服务重启时订单状态不确定，需要人工核对")
            return
        if state.restored and state.payload.mode == "live" and state.status == "position_open":
            reconcile_restored_live_position(state)
            add_strategy_event(state, "position_verified", "服务重启后已核对 Binance 实际持仓")
        for current_price, price_ts, feed_state in strategy_price_stream(state):
            state.current_price = current_price
            state.current_price_ts = price_ts
            state.feed_state = feed_state
            state.entry_line_price = state.payload.entry_line.price_at(price_ts)
            state.stop_line_price = state.payload.stop_line.price_at(price_ts) if state.payload.stop_line else None
            state.updated_ts = int(time.time() * 1000)
            if time.monotonic() - state.last_persist_monotonic >= 1:
                persist_strategy(state)
                state.last_persist_monotonic = time.monotonic()
            if state.error and state.error.startswith("行情暂时不可用"):
                state.error = None

            if state.cancel_event.is_set():
                break
            if state.status == "armed":
                validate_stop_side(state.payload, current_price, price_ts)
                if not should_enter(state.payload, current_price, state.entry_line_price):
                    continue
                update_strategy_status(state, "entering", "入场线已触发，正在提交市价单", trigger_price=current_price)
                if state.payload.mode == "live":
                    live_strategy_preflight(state.payload, configure=True)
                    order = submit_futures_market_order(state, closing=False, reference_price=current_price)
                    fallback_qty = normalize_futures_quantity(state.payload.symbol, state.payload.notional_usdt, current_price)
                    state.filled_qty, state.entry_price = order_fill_values(order, current_price, fallback_qty)
                    state.entry_order = order
                else:
                    state.filled_qty = state.payload.notional_usdt / current_price
                    state.entry_price = current_price
                    state.entry_order = {"simulated": True, "side": "BUY" if state.payload.direction == "LONG" else "SELL"}
                position_message = "入场成交，开始监控止损" if state.payload.stop_line else "入场成交；未设置自动止损，等待手动处理"
                update_strategy_status(state, "position_open", position_message, entry_price=state.entry_price, quantity=state.filled_qty)
                notify_strategy_bark(state, "open")

            if (
                state.status == "position_open"
                and state.payload.stop_line is not None
                and state.stop_line_price is not None
                and should_stop(state.payload, current_price, state.stop_line_price)
            ):
                add_strategy_event(state, "stop_triggered", "止损线已触发", trigger_price=current_price, line_price=state.stop_line_price)
                close_strategy_position(state, current_price)
                return

        if state.status == "position_open":
            if state.close_on_cancel:
                price = state.current_price or fetch_futures_price(state.payload.symbol)
                close_strategy_position(state, price, cancelled=True)
            else:
                update_strategy_status(state, "position_left_open", "监控已停止，仓位保持开启")
        elif state.status in {"armed", "entering"}:
            update_strategy_status(state, "cancelled", "策略已取消")
    except ValueError as exc:
        state.error = str(exc)
        update_strategy_status(state, "failed", f"策略校验失败：{exc}")
    except Exception as exc:
        logger.exception("Futures strategy failed: strategy_id=%s", strategy_id)
        state.error = str(exc)
        update_strategy_status(state, "attention_required" if state.status in {"entering", "position_open", "exiting"} else "failed", f"策略运行失败：{exc}")


def start_strategy_thread(state: FuturesStrategyState) -> None:
    thread = threading.Thread(target=run_futures_strategy, args=(state.strategy_id,), daemon=True)
    thread.start()


def restore_persisted_strategies() -> None:
    init_strategy_db()
    with FUTURES_DB_LOCK, sqlite3.connect(get_strategy_db_path()) as conn:
        rows = conn.execute(
            "SELECT strategy_id, payload_json, state_json, status FROM futures_strategies ORDER BY updated_ts DESC LIMIT 50"
        ).fetchall()
    for strategy_id, payload_json, state_json, status in rows:
        try:
            payload = FuturesStrategyRequest.model_validate_json(payload_json)
            saved = json.loads(state_json)
            state = FuturesStrategyState(
                strategy_id=strategy_id,
                payload=payload,
                status=status,
                created_ts=int(saved.get("created_ts") or time.time() * 1000),
                updated_ts=int(saved.get("updated_ts") or time.time() * 1000),
                current_price=to_finite_number(saved.get("current_price")),
                current_price_ts=to_int(saved.get("current_price_ts")),
                entry_line_price=to_finite_number(saved.get("entry_line_price")),
                stop_line_price=to_finite_number(saved.get("stop_line_price")),
                feed_state=str(saved.get("feed_state") or "connecting"),
                filled_qty=to_finite_number(saved.get("filled_qty")),
                entry_price=to_finite_number(saved.get("entry_price")),
                exit_price=to_finite_number(saved.get("exit_price")),
                entry_order=saved.get("entry_order"),
                exit_order=saved.get("exit_order"),
                error=str(saved.get("error")) if saved.get("error") else None,
                events=list(saved.get("events") or []),
                restored=True,
            )
            with FUTURES_STRATEGIES_LOCK:
                FUTURES_STRATEGIES[strategy_id] = state
            if status in ACTIVE_FUTURES_STRATEGY_STATUSES:
                start_strategy_thread(state)
        except Exception:
            logger.exception("Failed to restore futures strategy: %s", strategy_id)


@app.on_event("startup")
def start_futures_strategy_runtime() -> None:
    restore_persisted_strategies()


@app.get("/market/klines")
def market_klines(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    limit: int = 500,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
):
    if timeframe not in INTERVAL_TO_MS:
        raise HTTPException(status_code=400, detail="unsupported timeframe")
    try:
        return fetch_futures_klines(symbol.strip().upper(), timeframe, limit, start_time, end_time)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/settings/bark")
def read_bark_settings():
    return get_bark_settings().model_dump()


@app.put("/settings/bark")
def update_bark_settings(payload: BarkSettingsUpdate):
    try:
        return save_bark_settings(payload).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/settings/bark/test")
def test_bark_settings(payload: BarkSettingsUpdate):
    if not payload.endpoint:
        raise HTTPException(status_code=400, detail="请先填写 Bark 地址")
    try:
        send_bark_message(payload.endpoint, "BTC Breakout", "Bark 开平仓通知测试成功")
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Bark 测试失败：{exc}") from exc


@app.post("/strategy/watch")
def create_futures_strategy(payload: FuturesStrategyRequest):
    with FUTURES_STRATEGIES_LOCK:
        if any(
            item.payload.symbol == payload.symbol
            and item.payload.mode == "live"
            and item.status in ACTIVE_FUTURES_STRATEGY_STATUSES
            for item in FUTURES_STRATEGIES.values()
        ) and payload.mode == "live":
            raise HTTPException(status_code=409, detail=f"{payload.symbol} 已有活动 live 策略")
    try:
        if payload.mode == "live":
            live_strategy_preflight(payload, configure=False)
            current_price = fetch_futures_price(payload.symbol)
        else:
            try:
                current_price = fetch_futures_price(payload.symbol)
            except Exception:
                current_price = fetch_spot_reference_price(payload.symbol)
        now = int(time.time() * 1000)
        entry_line_price, stop_line_price = validate_stop_side(payload, current_price, now)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    strategy_id = uuid.uuid4().hex
    state = FuturesStrategyState(
        strategy_id=strategy_id,
        payload=payload,
        status="armed",
        created_ts=now,
        updated_ts=now,
        current_price=current_price,
        current_price_ts=now,
        entry_line_price=entry_line_price,
        stop_line_price=stop_line_price,
    )
    armed_message = "策略已启用，正在等待入场" if payload.stop_line else "策略已启用，正在等待入场；未设置自动止损"
    state.events.append({"ts": now, "type": "status", "message": armed_message, "details": {"status": "armed"}})
    with FUTURES_STRATEGIES_LOCK:
        FUTURES_STRATEGIES[strategy_id] = state
    persist_strategy(state)
    start_strategy_thread(state)
    return strategy_public_dict(state)


@app.get("/strategy/watch")
def list_futures_strategies():
    with FUTURES_STRATEGIES_LOCK:
        states = sorted(FUTURES_STRATEGIES.values(), key=lambda item: item.created_ts, reverse=True)
        return [strategy_public_dict(item) for item in states[:50]]


@app.get("/strategy/watch/{strategy_id}")
def get_futures_strategy(strategy_id: str):
    with FUTURES_STRATEGIES_LOCK:
        state = FUTURES_STRATEGIES.get(strategy_id)
        if state is None:
            raise HTTPException(status_code=404, detail="strategy not found")
        return strategy_public_dict(state)


@app.delete("/strategy/watch/{strategy_id}")
def cancel_futures_strategy(strategy_id: str, close_position: Optional[bool] = None):
    with FUTURES_STRATEGIES_LOCK:
        state = FUTURES_STRATEGIES.get(strategy_id)
        if state is None:
            raise HTTPException(status_code=404, detail="strategy not found")
        if state.status not in ACTIVE_FUTURES_STRATEGY_STATUSES:
            return strategy_public_dict(state)
        if state.status in {"position_open", "exiting"} and close_position is None:
            raise HTTPException(status_code=409, detail="position is open; choose close_position=true or false")
        state.close_on_cancel = bool(close_position)
        state.cancel_event.set()
        add_strategy_event(state, "cancel_requested", "用户请求取消策略", close_position=state.close_on_cancel)
        return strategy_public_dict(state)
