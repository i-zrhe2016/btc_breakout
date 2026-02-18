from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Optional
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator


app = FastAPI(title="Trendline Breakout API", version="0.1.0")


DEFAULT_BARK_NOTIFY_URL = "https://api.day.app/j32eBocVfwx6kvf8xr452K/"
LOG_LEVEL = str(os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trendline_api")


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
    url_base = str(base_url or os.getenv("BINANCE_BASE_URL") or "https://api.binance.us").rstrip("/")
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

    url_base = str(payload.base_url or os.getenv("BINANCE_BASE_URL") or "https://api.binance.us").rstrip("/")
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
