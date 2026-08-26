<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
} from "lightweight-charts";

const chartHost = ref(null);
const chartStage = ref(null);
const confirmDialog = ref(null);
const symbol = ref("BTCUSDT");
const timeframe = ref("1h");
const snap = ref(true);
const tool = ref("select");
const lines = ref([]);
const selectedLineId = ref(null);
const entryLineId = ref(null);
const stopLineId = ref(null);
const direction = ref("LONG");
const notional = ref(100);
const leverage = ref(30);
const runMode = ref("simulate");
const candles = ref([]);
const loading = ref(false);
const feedState = ref("loading");
const chartMessage = ref("正在加载 K 线…");
const currentStrategy = ref(null);
const undoStack = ref([]);
const draft = ref(null);
const activeDrag = ref(null);
const overlayTick = ref(0);
const viewport = reactive({ width: 1, height: 1, left: 0, right: 1, bottom: 1 });

const lineColors = ["#27d3ee", "#f5bf45", "#a78bfa", "#36d483", "#ff8b5e", "#ff6b78", "#60a5fa", "#f472b6"];
const storageKey = "btc-breakout-vue-workspace-v2";
const terminalStatuses = new Set(["completed", "cancelled", "failed", "attention_required", "position_left_open"]);
const supportedTimeframes = new Set(["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"]);

let chart = null;
let candleSeries = null;
let resizeObserver = null;
let refreshTimer = null;
let pollTimer = null;

function formatPrice(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString("en-US", { maximumFractionDigits: number >= 1000 ? 2 : 6 });
}

function formatTs(ts) {
  return Number.isFinite(Number(ts))
    ? new Date(Number(ts)).toLocaleString("zh-CN", { hour12: false })
    : "--";
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function intervalMs() {
  if (candles.value.length > 1) {
    const value = Number(candles.value.at(-1).ts) - Number(candles.value.at(-2).ts);
    if (Number.isFinite(value) && value > 0) return value;
  }
  const units = { m: 60000, h: 3600000, d: 86400000, w: 604800000 };
  const match = String(timeframe.value).match(/^(\d+)([mhdw])$/);
  return match ? Number(match[1]) * units[match[2]] : 3600000;
}

function normalLine(line, index = 0) {
  const next = {
    ...line,
    id: String(line.id || `line-${Date.now()}-${Math.random().toString(16).slice(2)}`),
    color: line.color || lineColors[index % lineColors.length],
    direction: line.direction === "down" ? "down" : "up",
    visible: line.visible !== false,
  };
  if (next.kind === "horizontal") {
    next.price = Number(next.price);
    return next;
  }
  next.ts1 = Number(next.ts1);
  next.ts2 = Number(next.ts2);
  next.price1 = Number(next.price1);
  next.price2 = Number(next.price2);
  if (next.ts1 > next.ts2) {
    [next.ts1, next.price1, next.ts2, next.price2] = [next.ts2, next.price2, next.ts1, next.price1];
  }
  return next;
}

function validLine(line) {
  if (!line || !["horizontal", "trendline"].includes(line.kind)) return false;
  if (line.kind === "horizontal") return Number(line.price) > 0;
  return Number(line.ts1) !== Number(line.ts2) && Number(line.price1) > 0 && Number(line.price2) > 0;
}

function priceAt(line, ts) {
  if (!line) return null;
  if (line.kind === "horizontal") return Number(line.price);
  const span = Number(line.ts2) - Number(line.ts1);
  if (!span) return null;
  return Number(line.price1) + (Number(line.price2) - Number(line.price1)) * ((Number(ts) - Number(line.ts1)) / span);
}

function lineLabel(line) {
  return line?.kind === "horizontal"
    ? `水平线 · ${formatPrice(line.price)}`
    : `趋势线 · ${formatPrice(line?.price1)} → ${formatPrice(line?.price2)}`;
}

function lineById(id) {
  return lines.value.find((line) => line.id === id) || null;
}

function suggestedDirection(kind, price1, price2) {
  return kind === "horizontal" ? "up" : Number(price2) >= Number(price1) ? "down" : "up";
}

function saveWorkspace() {
  localStorage.setItem(storageKey, JSON.stringify({
    symbol: symbol.value,
    timeframe: timeframe.value,
    snap: snap.value,
    lines: lines.value,
    selectedLineId: selectedLineId.value,
    entryLineId: entryLineId.value,
    stopLineId: stopLineId.value,
    direction: direction.value,
    notional: notional.value,
    leverage: leverage.value,
    runMode: runMode.value,
  }));
}

function restoreWorkspace() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (!saved) return;
    if (saved.symbol) symbol.value = String(saved.symbol).toUpperCase();
    if (supportedTimeframes.has(saved.timeframe)) timeframe.value = saved.timeframe;
    if (typeof saved.snap === "boolean") snap.value = saved.snap;
    if (Array.isArray(saved.lines)) lines.value = saved.lines.filter(validLine).map(normalLine);
    selectedLineId.value = saved.selectedLineId || lines.value[0]?.id || null;
    entryLineId.value = saved.entryLineId || lines.value[0]?.id || null;
    stopLineId.value = saved.stopLineId || null;
    direction.value = saved.direction === "SHORT" ? "SHORT" : "LONG";
    if (Number(saved.notional) > 0) notional.value = Number(saved.notional);
    if (Number(saved.leverage) > 0) leverage.value = Number(saved.leverage);
    if (["simulate", "live"].includes(saved.runMode)) runMode.value = saved.runMode;
  } catch (_) {
    localStorage.removeItem(storageKey);
  }
}

function snapshot() {
  return JSON.stringify({
    lines: lines.value,
    selectedLineId: selectedLineId.value,
    entryLineId: entryLineId.value,
    stopLineId: stopLineId.value,
  });
}

function pushUndo() {
  undoStack.value.push(snapshot());
  if (undoStack.value.length > 30) undoStack.value.shift();
}

function undo() {
  const previous = undoStack.value.pop();
  if (!previous) return;
  const state = JSON.parse(previous);
  lines.value = state.lines.map(normalLine);
  selectedLineId.value = state.selectedLineId;
  entryLineId.value = state.entryLineId;
  stopLineId.value = state.stopLineId;
  saveWorkspace();
  refreshOverlay();
}

function rightOffset() {
  const width = chartHost.value?.clientWidth || window.innerWidth;
  return width < 560 ? 7 : width < 920 ? 11 : 18;
}

function refreshOverlay() {
  if (!chartHost.value) return;
  viewport.width = Math.max(1, chartHost.value.clientWidth);
  viewport.height = Math.max(1, chartHost.value.clientHeight);
  viewport.left = 0;
  const timeScaleWidth = Number(chart?.timeScale().width?.());
  const timeScaleHeight = Number(chart?.timeScale().height?.());
  viewport.right = Number.isFinite(timeScaleWidth) && timeScaleWidth > 1 ? Math.min(timeScaleWidth, viewport.width) : viewport.width;
  viewport.bottom = Math.max(1, viewport.height - (Number.isFinite(timeScaleHeight) ? timeScaleHeight : 28));
  overlayTick.value += 1;
}

function xForTs(ts) {
  const first = candles.value[0];
  if (!chart || !first) return NaN;
  return Number(chart.timeScale().logicalToCoordinate((Number(ts) - Number(first.ts)) / intervalMs()));
}

function yForPrice(price) {
  return Number(candleSeries?.priceToCoordinate(Number(price)));
}

function visibleTimeBounds() {
  const first = candles.value[0];
  const range = chart?.timeScale().getVisibleLogicalRange?.();
  if (first && range && Number.isFinite(Number(range.from)) && Number.isFinite(Number(range.to))) {
    return {
      from: Number(first.ts) + Number(range.from) * intervalMs(),
      to: Number(first.ts) + Number(range.to) * intervalMs(),
    };
  }
  return { from: Number(first?.ts || Date.now() - 86400000), to: Number(candles.value.at(-1)?.ts || Date.now()) };
}

function clipInfiniteLine(a, b) {
  const left = viewport.left;
  const right = viewport.right;
  const top = 0;
  const bottom = viewport.bottom;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (![a.x, a.y, b.x, b.y, dx, dy].every(Number.isFinite) || (Math.abs(dx) < 0.0001 && Math.abs(dy) < 0.0001)) return null;
  const points = [];
  const add = (x, y) => {
    if (x < left - 0.5 || x > right + 0.5 || y < top - 0.5 || y > bottom + 0.5) return;
    if (!points.some((point) => Math.hypot(point.x - x, point.y - y) < 0.5)) points.push({ x, y });
  };
  if (Math.abs(dx) > 0.0001) {
    let t = (left - a.x) / dx; add(left, a.y + t * dy);
    t = (right - a.x) / dx; add(right, a.y + t * dy);
  }
  if (Math.abs(dy) > 0.0001) {
    let t = (top - a.y) / dy; add(a.x + t * dx, top);
    t = (bottom - a.y) / dy; add(a.x + t * dx, bottom);
  }
  if (points.length < 2) return null;
  let pair = [points[0], points[1]];
  let distance = -1;
  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      const nextDistance = Math.hypot(points[j].x - points[i].x, points[j].y - points[i].y);
      if (nextDistance > distance) { distance = nextDistance; pair = [points[i], points[j]]; }
    }
  }
  return { x1: pair[0].x, y1: pair[0].y, x2: pair[1].x, y2: pair[1].y };
}

const visualLines = computed(() => {
  void overlayTick.value;
  return lines.value.flatMap((line) => {
    if (!line.visible) return [];
    let a;
    let b;
    if (line.kind === "horizontal") {
      const y = yForPrice(line.price);
      a = { x: viewport.left, y };
      b = { x: viewport.right, y };
    } else {
      a = { x: xForTs(line.ts1), y: yForPrice(line.price1) };
      b = { x: xForTs(line.ts2), y: yForPrice(line.price2) };
    }
    const segment = clipInfiniteLine(a, b);
    if (!segment) return [];
    const anchors = line.kind === "trendline"
      ? [a, b].map((point, index) => ({ ...point, index })).filter((point) => point.x >= viewport.left - 1 && point.x <= viewport.right + 1 && point.y >= -1 && point.y <= viewport.bottom + 1)
      : [];
    return [{ line, segment, anchors }];
  });
});

const draftSegment = computed(() => {
  void overlayTick.value;
  if (!draft.value?.start || !draft.value?.current) return null;
  return {
    x1: draft.value.start.x,
    y1: draft.value.start.y,
    x2: draft.value.current.x,
    y2: draft.value.current.y,
  };
});

function rawPointFromEvent(event) {
  const rect = chartStage.value.getBoundingClientRect();
  const x = clamp(event.clientX - rect.left, viewport.left, viewport.right);
  const y = clamp(event.clientY - rect.top, 0, viewport.bottom);
  const logical = chart?.timeScale().coordinateToLogical(x);
  const first = candles.value[0];
  const ts = first && logical != null && Number.isFinite(Number(logical))
    ? Number(first.ts) + Number(logical) * intervalMs()
    : NaN;
  const price = candleSeries?.coordinateToPrice(y);
  return { x, y, ts, price: price == null ? NaN : Number(price), snapped: false };
}

function nearestCandleIndex(ts) {
  if (!candles.value.length || !Number.isFinite(Number(ts))) return -1;
  let best = 0;
  let distance = Infinity;
  candles.value.forEach((candle, index) => {
    const next = Math.abs(Number(candle.ts) - Number(ts));
    if (next < distance) { distance = next; best = index; }
  });
  return best;
}

function snapPoint(point) {
  if (!snap.value || !Number.isFinite(point.price)) return point;
  const center = nearestCandleIndex(point.ts);
  if (center < 0) return point;
  let closest = null;
  let closestDistance = 16;
  for (let index = Math.max(0, center - 2); index <= Math.min(candles.value.length - 1, center + 2); index += 1) {
    const candle = candles.value[index];
    const x = xForTs(candle.ts);
    for (const field of ["open", "high", "low", "close"]) {
      const price = Number(candle[field]);
      const y = yForPrice(price);
      const distance = Math.hypot(x - point.x, y - point.y);
      if (Number.isFinite(distance) && distance < closestDistance) {
        closestDistance = distance;
        closest = { ...point, x, y, ts: candle.ts, price, snapped: true };
      }
    }
  }
  return closest || point;
}

function pointFromEvent(event, shouldSnap = false) {
  const point = rawPointFromEvent(event);
  return shouldSnap ? snapPoint(point) : point;
}

function setTool(nextTool) {
  if (activeDrag.value) cancelInteraction();
  draft.value = null;
  tool.value = nextTool;
}

function addLine(line) {
  pushUndo();
  const next = normalLine({ ...line, color: lineColors[lines.value.length % lineColors.length] }, lines.value.length);
  lines.value.push(next);
  selectedLineId.value = next.id;
  if (!entryLineId.value) entryLineId.value = next.id;
  saveWorkspace();
  refreshOverlay();
}

function finalizeTrendline(end) {
  const start = draft.value?.start;
  if (!start || !end || ![start.ts, start.price, end.ts, end.price].every(Number.isFinite)) return false;
  if (Math.abs(end.x - start.x) < 18 || Math.abs(end.ts - start.ts) < intervalMs() * 0.5 || start.price <= 0 || end.price <= 0) {
    draft.value.error = "两个锚点至少间隔一根 K 线";
    draft.value.current = end;
    return false;
  }
  addLine({
    kind: "trendline",
    ts1: start.ts,
    price1: start.price,
    ts2: end.ts,
    price2: end.price,
    direction: suggestedDirection("trendline", start.price, end.price),
  });
  draft.value = null;
  tool.value = "select";
  return true;
}

function onDrawingPointerDown(event) {
  if (event.button !== 0) return;
  event.preventDefault();
  const point = pointFromEvent(event, snap.value);
  if (!Number.isFinite(point.price) || point.price <= 0) return;
  if (tool.value === "horizontal") {
    addLine({ kind: "horizontal", price: point.price, direction: "up" });
    tool.value = "select";
    return;
  }
  if (draft.value?.awaitingSecond) {
    finalizeTrendline(point);
    return;
  }
  draft.value = { start: point, current: point, pressing: true, moved: false, awaitingSecond: false, error: "" };
}

function clampAnchorTime(line, anchorIndex, requested) {
  const range = visibleTimeBounds();
  const gap = intervalMs();
  if (anchorIndex === 0) return clamp(requested, range.from, Math.min(range.to, Number(line.ts2) - gap));
  return clamp(requested, Math.max(range.from, Number(line.ts1) + gap), range.to);
}

function clampLineDelta(line, requested) {
  const range = visibleTimeBounds();
  const first = Math.min(Number(line.ts1), Number(line.ts2));
  const last = Math.max(Number(line.ts1), Number(line.ts2));
  if (first >= range.from && last <= range.to && last - first <= range.to - range.from) {
    return clamp(requested, range.from - first, range.to - last);
  }
  return clamp(requested, range.from - last, range.to - first);
}

function startDrag(line, anchorIndex, event) {
  if (event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  selectedLineId.value = line.id;
  pushUndo();
  activeDrag.value = {
    lineId: line.id,
    anchorIndex,
    start: pointFromEvent(event),
    original: JSON.parse(JSON.stringify(line)),
    moved: false,
  };
}

function onGlobalPointerMove(event) {
  if (draft.value) {
    if (!draft.value.pressing && !draft.value.awaitingSecond) return;
    const point = pointFromEvent(event, snap.value);
    draft.value.current = point;
    if (draft.value.pressing && Math.hypot(point.x - draft.value.start.x, point.y - draft.value.start.y) > 6) draft.value.moved = true;
    draft.value.error = "";
    return;
  }
  if (!activeDrag.value) return;
  const line = lineById(activeDrag.value.lineId);
  if (!line) return;
  event.preventDefault();
  const point = pointFromEvent(event, snap.value && activeDrag.value.anchorIndex !== null);
  const original = activeDrag.value.original;
  activeDrag.value.moved = true;
  if (line.kind === "horizontal") {
    if (Number.isFinite(point.price)) line.price = Math.max(0.00000001, point.price);
  } else if (activeDrag.value.anchorIndex === 0) {
    line.ts1 = clampAnchorTime(line, 0, point.ts);
    if (Number.isFinite(point.price)) line.price1 = Math.max(0.00000001, point.price);
  } else if (activeDrag.value.anchorIndex === 1) {
    line.ts2 = clampAnchorTime(line, 1, point.ts);
    if (Number.isFinite(point.price)) line.price2 = Math.max(0.00000001, point.price);
  } else {
    const deltaTs = clampLineDelta(original, point.ts - activeDrag.value.start.ts);
    const requestedPriceDelta = point.price - activeDrag.value.start.price;
    const minimumPriceDelta = 0.00000001 - Math.min(Number(original.price1), Number(original.price2));
    const deltaPrice = Number.isFinite(requestedPriceDelta) ? Math.max(requestedPriceDelta, minimumPriceDelta) : 0;
    line.ts1 = Number(original.ts1) + deltaTs;
    line.ts2 = Number(original.ts2) + deltaTs;
    line.price1 = Number(original.price1) + deltaPrice;
    line.price2 = Number(original.price2) + deltaPrice;
  }
  refreshOverlay();
}

function onGlobalPointerUp() {
  if (draft.value?.pressing) {
    draft.value.pressing = false;
    if (draft.value.moved) finalizeTrendline(draft.value.current);
    else draft.value.awaitingSecond = true;
    return;
  }
  if (activeDrag.value) {
    activeDrag.value = null;
    saveWorkspace();
    refreshOverlay();
  }
}

function cancelInteraction() {
  if (activeDrag.value) {
    const line = lineById(activeDrag.value.lineId);
    if (line) Object.assign(line, activeDrag.value.original);
    activeDrag.value = null;
  }
  draft.value = null;
  saveWorkspace();
  refreshOverlay();
}

function selectLine(id) {
  selectedLineId.value = id;
  saveWorkspace();
}

function deleteLine(id) {
  if (!lineById(id)) return;
  pushUndo();
  lines.value = lines.value.filter((line) => line.id !== id);
  if (selectedLineId.value === id) selectedLineId.value = lines.value[0]?.id || null;
  if (entryLineId.value === id) entryLineId.value = lines.value[0]?.id || null;
  if (stopLineId.value === id) stopLineId.value = null;
  saveWorkspace();
  refreshOverlay();
}

function clearLines() {
  if (!lines.value.length) return;
  pushUndo();
  lines.value = [];
  selectedLineId.value = null;
  entryLineId.value = null;
  stopLineId.value = null;
  saveWorkspace();
  refreshOverlay();
}

function assignRole(id, role) {
  const line = lineById(id);
  if (!line) return;
  if (role === "entry") {
    entryLineId.value = id;
    direction.value = line.direction === "down" ? "SHORT" : "LONG";
  } else stopLineId.value = stopLineId.value === id ? null : id;
  saveWorkspace();
}

function toggleLineDirection(line) {
  pushUndo();
  line.direction = line.direction === "up" ? "down" : "up";
  if (entryLineId.value === line.id) direction.value = line.direction === "down" ? "SHORT" : "LONG";
  saveWorkspace();
}

function nudgeLine(line, bars) {
  if (line.kind !== "trendline") return;
  pushUndo();
  const delta = clampLineDelta(line, bars * intervalMs());
  line.ts1 += delta;
  line.ts2 += delta;
  selectedLineId.value = line.id;
  saveWorkspace();
  refreshOverlay();
}

function detectBreakout(line) {
  if (!line || !candles.value.length) return null;
  const startTs = line.kind === "trendline" ? Math.max(Number(line.ts1), Number(line.ts2)) : Number(candles.value[0].ts);
  let previous = null;
  for (const candle of candles.value) {
    if (Number(candle.ts) < startTs) continue;
    const linePrice = priceAt(line, candle.ts);
    if (!Number.isFinite(linePrice)) continue;
    const touched = line.direction === "up" ? Number(candle.high) >= linePrice : Number(candle.low) <= linePrice;
    const previousPrice = previous ? priceAt(line, previous.ts) : null;
    const wasBeyond = previous && (line.direction === "up" ? Number(previous.high) < previousPrice : Number(previous.low) > previousPrice);
    if (touched && (!previous || wasBeyond)) return { candle, linePrice };
    previous = candle;
  }
  return null;
}

const selectedLine = computed(() => lineById(selectedLineId.value));
const selectedBreakout = computed(() => detectBreakout(selectedLine.value));
const latestCandle = computed(() => candles.value.at(-1) || null);
const selectedLinePrice = computed(() => selectedLine.value && latestCandle.value ? priceAt(selectedLine.value, latestCandle.value.ts) : null);
const drawingHint = computed(() => {
  if (tool.value === "trendline") {
    if (draft.value?.error) return `趋势线 · ${draft.value.error} · Esc 取消`;
    if (draft.value?.awaitingSecond) return "趋势线 · 移动预览并单击第二锚点 · Esc 取消";
    if (draft.value) return "趋势线 · 拖到第二锚点后松开 · Esc 取消";
    return "趋势线 · 点击两个锚点，或按住拖动完成 · Esc 取消";
  }
  if (tool.value === "horizontal") return "水平线 · 点击价格位置放置 · Esc 取消";
  return "选择工具 · 拖动锚点或整条线；←/→ 可按一根 K 线移动";
});

const formErrors = computed(() => {
  const errors = [];
  const entry = lineById(entryLineId.value);
  const stop = lineById(stopLineId.value);
  if (!entry) errors.push("请先画线并设置入场线");
  if (entry && stop && entry.id === stop.id) errors.push("入场线和止损线不能相同");
  if (!(Number(notional.value) > 0)) errors.push("名义仓位必须大于 0");
  if (!Number.isInteger(Number(leverage.value)) || Number(leverage.value) < 1 || Number(leverage.value) > 125) errors.push("杠杆必须是 1–125 的整数");
  return errors;
});

const canArm = computed(() => formErrors.value.length === 0 && (!currentStrategy.value || terminalStatuses.has(currentStrategy.value.status)));

function lineForApi(line) {
  if (!line) return null;
  return line.kind === "horizontal"
    ? { kind: "horizontal", price: Number(line.price) }
    : { kind: "trendline", ts1: Number(line.ts1), price1: Number(line.price1), ts2: Number(line.ts2), price2: Number(line.price2) };
}

function strategyPayload() {
  return {
    symbol: symbol.value.trim().toUpperCase(),
    timeframe: timeframe.value,
    chart_timezone: "Asia/Shanghai",
    direction: direction.value,
    notional_usdt: Number(notional.value),
    leverage: Number(leverage.value),
    mode: runMode.value,
    entry_line: lineForApi(lineById(entryLineId.value)),
    stop_line: lineForApi(lineById(stopLineId.value)),
  };
}

function openConfirm() {
  if (!canArm.value) return;
  confirmDialog.value?.showModal();
}

async function armStrategy() {
  try {
    const response = await fetch("/strategy/watch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(strategyPayload()) });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.detail || `HTTP ${response.status}`);
    currentStrategy.value = result;
    confirmDialog.value?.close();
    startPolling(result.strategy_id);
  } catch (error) {
    chartMessage.value = `策略启用失败：${error.message || error}`;
  }
}

function startPolling(id) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const response = await fetch(`/strategy/watch/${id}`);
      if (!response.ok) return;
      currentStrategy.value = await response.json();
      if (terminalStatuses.has(currentStrategy.value.status)) clearInterval(pollTimer);
    } catch (_) { /* next poll retries */ }
  }, 2000);
}

async function restoreLatestStrategy() {
  try {
    const response = await fetch("/strategy/watch");
    if (!response.ok) return;
    const items = await response.json();
    const active = items.find((item) => !terminalStatuses.has(item.status));
    if (active) { currentStrategy.value = active; startPolling(active.strategy_id); }
  } catch (_) { /* optional history */ }
}

async function cancelStrategy() {
  if (!currentStrategy.value) return;
  try {
    const response = await fetch(`/strategy/watch/${currentStrategy.value.strategy_id}`, { method: "DELETE" });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.detail || `HTTP ${response.status}`);
    currentStrategy.value = result;
  } catch (error) {
    chartMessage.value = `取消失败：${error.message || error}`;
  }
}

async function loadChart({ fit = false } = {}) {
  if (loading.value) return;
  loading.value = true;
  feedState.value = "loading";
  chartMessage.value = `正在加载 ${symbol.value} ${timeframe.value} K 线…`;
  try {
    const response = await fetch(`/market/klines?symbol=${encodeURIComponent(symbol.value.trim().toUpperCase())}&timeframe=${encodeURIComponent(timeframe.value)}&limit=500`);
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.detail || `HTTP ${response.status}`);
    if (!Array.isArray(result) || !result.length) throw new Error("行情源返回空数据");
    candles.value = result.map((item) => ({ ...item, ts: Number(item.ts), time: Math.floor(Number(item.ts) / 1000) }));
    candleSeries.setData(candles.value.map((candle) => ({ time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close })));
    if (fit) chart.timeScale().fitContent();
    chart.applyOptions({ timeScale: { rightOffset: rightOffset() } });
    feedState.value = result[0]?.source === "binance_spot_fallback" ? "reference" : "live";
    chartMessage.value = result[0]?.source === "binance_spot_fallback" ? "Futures 不可用 · 使用 Spot 参考 K 线" : "Futures K 线";
    requestAnimationFrame(refreshOverlay);
  } catch (error) {
    candles.value = [];
    feedState.value = "error";
    chartMessage.value = `K 线加载失败：${error.message || error}`;
    refreshOverlay();
  } finally {
    loading.value = false;
  }
}

function fitChart() {
  chart?.timeScale().fitContent();
  chart?.applyOptions({ timeScale: { rightOffset: rightOffset() } });
  requestAnimationFrame(refreshOverlay);
}

function setDirection(next) {
  direction.value = next;
  const entry = lineById(entryLineId.value);
  if (entry) entry.direction = next === "SHORT" ? "down" : "up";
  saveWorkspace();
}

function handleKeydown(event) {
  const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(event.target?.tagName);
  if (event.key === "Escape" && (draft.value || activeDrag.value || tool.value !== "select")) {
    cancelInteraction();
    tool.value = "select";
    event.preventDefault();
    return;
  }
  if (editing || tool.value !== "select" || !selectedLine.value) return;
  if (event.key === "Delete" || event.key === "Backspace") {
    deleteLine(selectedLine.value.id);
    event.preventDefault();
  } else if (selectedLine.value.kind === "trendline" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    nudgeLine(selectedLine.value, event.key === "ArrowLeft" ? -1 : 1);
    event.preventDefault();
  }
}

onMounted(async () => {
  restoreWorkspace();
  await nextTick();
  chart = createChart(chartHost.value, {
    width: chartHost.value.clientWidth,
    height: chartHost.value.clientHeight,
    layout: { background: { type: ColorType.Solid, color: "#070b15" }, textColor: "#a5b4ca", fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: "#111b2c" }, horzLines: { color: "#111b2c" } },
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: "#536987", width: 1, style: 3 }, horzLine: { color: "#536987", width: 1, style: 3 } },
    rightPriceScale: { borderColor: "#273753", scaleMargins: { top: 0.08, bottom: 0.08 } },
    timeScale: { borderColor: "#273753", timeVisible: true, secondsVisible: false, rightOffset: rightOffset(), barSpacing: 8, minBarSpacing: 2 },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
  });
  candleSeries = chart.addSeries(CandlestickSeries, { upColor: "#36d483", downColor: "#ff6b78", borderVisible: false, wickUpColor: "#36d483", wickDownColor: "#ff6b78", priceLineVisible: true, lastValueVisible: true });
  chart.timeScale().subscribeVisibleLogicalRangeChange(refreshOverlay);
  resizeObserver = new ResizeObserver(() => {
    chart.applyOptions({ width: chartHost.value.clientWidth, height: chartHost.value.clientHeight, timeScale: { rightOffset: rightOffset() } });
    refreshOverlay();
  });
  resizeObserver.observe(chartHost.value);
  window.addEventListener("pointermove", onGlobalPointerMove, { passive: false });
  window.addEventListener("pointerup", onGlobalPointerUp);
  window.addEventListener("pointercancel", onGlobalPointerUp);
  window.addEventListener("keydown", handleKeydown);
  await loadChart({ fit: true });
  restoreLatestStrategy();
  refreshTimer = setInterval(() => loadChart(), 15000);
});

onBeforeUnmount(() => {
  clearInterval(refreshTimer);
  clearInterval(pollTimer);
  resizeObserver?.disconnect();
  chart?.remove();
  window.removeEventListener("pointermove", onGlobalPointerMove);
  window.removeEventListener("pointerup", onGlobalPointerUp);
  window.removeEventListener("pointercancel", onGlobalPointerUp);
  window.removeEventListener("keydown", handleKeydown);
});
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">B</span>
        <span><strong>BTC Breakout</strong><small>Vue manual drawing terminal</small></span>
      </div>
      <div class="top-actions">
        <span class="feed-pill" :class="feedState">{{ feedState === "live" ? "行情已连接" : feedState === "reference" ? "参考行情" : feedState === "error" ? "行情异常" : "行情连接中" }}</span>
        <a href="/settings">通知设置</a>
      </div>
    </header>

    <main class="workspace">
      <section class="chart-column" aria-label="K 线和手工画线区域">
        <div class="toolbar">
          <div class="toolbar-group market-controls">
            <label for="symbol">交易对</label>
            <input id="symbol" v-model.trim="symbol" autocomplete="off" @change="saveWorkspace(); loadChart({ fit: true })" />
            <label for="timeframe">周期</label>
            <select id="timeframe" v-model="timeframe" @change="saveWorkspace(); loadChart({ fit: true })">
              <option v-for="item in supportedTimeframes" :key="item" :value="item">{{ item }}</option>
            </select>
            <button class="tool-button" type="button" :disabled="loading" @click="loadChart()">{{ loading ? "加载中" : "刷新" }}</button>
          </div>
          <span class="toolbar-divider" aria-hidden="true"></span>
          <div class="toolbar-group drawing-tools" aria-label="画线工具">
            <button id="selectTool" class="tool-button" :class="{ active: tool === 'select' }" type="button" :aria-pressed="tool === 'select'" @click="setTool('select')">选择</button>
            <button id="trendTool" class="tool-button" :class="{ active: tool === 'trendline' }" type="button" :aria-pressed="tool === 'trendline'" @click="setTool('trendline')">趋势线</button>
            <button id="horizontalTool" class="tool-button" :class="{ active: tool === 'horizontal' }" type="button" :aria-pressed="tool === 'horizontal'" @click="setTool('horizontal')">水平线</button>
            <button id="snapTool" class="tool-button" :class="{ active: snap }" type="button" :aria-pressed="snap" @click="snap = !snap; saveWorkspace()">磁吸·{{ snap ? "开" : "关" }}</button>
            <button class="tool-button" type="button" :disabled="!undoStack.length" @click="undo">撤销</button>
            <button id="clearLines" class="tool-button danger" type="button" :disabled="!lines.length" @click="clearLines">清空线</button>
          </div>
          <span class="toolbar-spacer"></span>
          <button class="tool-button" type="button" @click="fitChart">全部 K 线</button>
        </div>

        <div id="chartStage" ref="chartStage" class="chart-stage" :class="{ drawing: tool !== 'select' }">
          <div ref="chartHost" class="chart-host"></div>
          <svg class="drawing-overlay" :viewBox="`0 0 ${viewport.width} ${viewport.height}`" preserveAspectRatio="none" aria-label="手工趋势线覆盖层">
            <g v-for="visual in visualLines" :key="visual.line.id">
              <line
                class="trend-line"
                :class="{ selected: visual.line.id === selectedLineId }"
                :x1="visual.segment.x1" :y1="visual.segment.y1" :x2="visual.segment.x2" :y2="visual.segment.y2"
                :stroke="visual.line.color"
              />
              <line
                v-if="tool === 'select'"
                class="line-hit"
                :x1="visual.segment.x1" :y1="visual.segment.y1" :x2="visual.segment.x2" :y2="visual.segment.y2"
                @pointerdown="startDrag(visual.line, null, $event)"
              />
              <circle
                v-for="anchor in visual.line.id === selectedLineId ? visual.anchors : []"
                :key="`hit-${anchor.index}`"
                class="anchor-hit"
                :cx="anchor.x" :cy="anchor.y" r="22"
                @pointerdown="startDrag(visual.line, anchor.index, $event)"
              />
              <circle
                v-for="anchor in visual.line.id === selectedLineId ? visual.anchors : []"
                :key="anchor.index"
                class="line-anchor"
                :cx="anchor.x" :cy="anchor.y" r="7"
                :stroke="visual.line.color"
                @pointerdown="startDrag(visual.line, anchor.index, $event)"
              />
            </g>
            <line
              v-if="draftSegment"
              class="draft-line"
              :x1="draftSegment.x1" :y1="draftSegment.y1" :x2="draftSegment.x2" :y2="draftSegment.y2"
            />
            <circle v-if="draft?.start" class="draft-anchor" :cx="draft.start.x" :cy="draft.start.y" r="5" />
            <circle v-if="draft?.current" class="draft-anchor" :cx="draft.current.x" :cy="draft.current.y" r="5" />
            <rect
              v-if="tool !== 'select'"
              class="drawing-surface"
              :x="viewport.left" y="0" :width="viewport.right - viewport.left" :height="viewport.bottom"
              @pointerdown="onDrawingPointerDown"
            />
          </svg>
          <div class="chart-hint">{{ drawingHint }}</div>
          <div v-if="!candles.length" class="chart-empty">{{ chartMessage }}</div>
        </div>

        <footer class="statusbar">
          <span class="status-main">{{ chartMessage }}</span>
          <span>{{ candles.length }} 根<span v-if="latestCandle"> · {{ formatTs(latestCandle.ts) }}</span></span>
          <span>{{ lines.length }} 条手工线</span>
        </footer>
      </section>

      <aside class="side-panel">
        <section class="side-section">
          <div class="section-head"><div><h1>手工画线</h1><p>Vue + SVG 矢量覆盖层</p></div><span class="badge">{{ lines.length }} 条</span></div>
          <div class="drawing-help">趋势线支持点击两锚点或按住拖动。拖动线体可整体平移，锚点和整线会停在右侧绘图区内；也可用 ←/→ 按一根 K 线移动。</div>
          <div class="lines-list">
            <div v-if="!lines.length" class="empty-list">还没有画线<br />从上方工具栏选择画线工具</div>
            <article
              v-for="(line, index) in lines"
              :key="line.id"
              class="line-card"
              :class="{ selected: line.id === selectedLineId }"
              :style="{ '--line-color': line.color }"
              :data-line-id="line.id"
            >
              <div class="line-card-head">
                <div class="line-name"><span class="line-swatch"></span><strong>{{ lineLabel(line) }}</strong></div>
                <span class="badge" :class="{ ok: detectBreakout(line) }">{{ detectBreakout(line) ? "已突破" : `#${index + 1}` }}</span>
              </div>
              <div class="line-card-meta"><span :class="line.direction === 'up' ? 'signal-up' : 'signal-down'">{{ line.direction === "up" ? "向上突破" : "向下突破" }}</span><span>{{ line.id === entryLineId ? "入场" : line.id === stopLineId ? "止损" : "未分配" }}</span></div>
              <div class="line-card-actions">
                <button class="mini-button" :class="{ selected: line.id === selectedLineId }" type="button" @click="selectLine(line.id)">选中</button>
                <button class="mini-button" :class="{ selected: line.id === entryLineId }" type="button" @click="assignRole(line.id, 'entry')">{{ line.id === entryLineId ? "入场线" : "设为入场" }}</button>
                <button class="mini-button" :class="{ selected: line.id === stopLineId }" type="button" @click="assignRole(line.id, 'stop')">{{ line.id === stopLineId ? "止损线" : "设为止损" }}</button>
                <button v-if="line.kind === 'trendline'" class="mini-button" type="button" aria-label="向左移动一根 K 线" @click="nudgeLine(line, -1)">← 1K</button>
                <button v-if="line.kind === 'trendline'" class="mini-button" type="button" aria-label="向右移动一根 K 线" @click="nudgeLine(line, 1)">1K →</button>
                <button class="mini-button" type="button" @click="toggleLineDirection(line)">{{ line.direction === "up" ? "改向下" : "改向上" }}</button>
                <button class="mini-button danger" type="button" @click="deleteLine(line.id)">删除</button>
              </div>
            </article>
          </div>
        </section>

        <section class="side-section">
          <div class="section-head"><div><h2>突破信号</h2><p>按手工线延伸计算历史与实时 K 线</p></div><span class="badge" :class="selectedBreakout ? 'ok' : 'warn'">{{ !selectedLine ? "等待画线" : selectedBreakout ? "已突破" : "未突破" }}</span></div>
          <div class="metrics">
            <div class="metric"><span>当前价格</span><strong>{{ formatPrice(latestCandle?.close) }}</strong></div>
            <div class="metric"><span>选中线价</span><strong>{{ formatPrice(selectedLinePrice) }}</strong></div>
          </div>
          <div class="breakout-box" :class="selectedBreakout ? 'ok' : selectedLine ? 'warn' : ''">
            <template v-if="!selectedLine">画线后会显示首次突破 K 线。</template>
            <template v-else-if="selectedBreakout">{{ lineLabel(selectedLine) }}<br />突破 K 线：{{ formatTs(selectedBreakout.candle.ts) }}<br />触碰价格：{{ formatPrice(selectedBreakout.linePrice) }}</template>
            <template v-else>{{ lineLabel(selectedLine) }}<br />当前数据范围内没有影线触碰。</template>
          </div>
        </section>

        <section class="side-section">
          <div class="section-head"><div><h2>策略参数</h2><p>选一条入场线，可选一条止损线</p></div><span class="badge" :class="formErrors.length ? 'warn' : 'ok'">{{ formErrors.length ? `${formErrors.length} 项待处理` : "可以启用" }}</span></div>
          <div class="field"><label>策略方向</label><div class="segmented"><button type="button" class="long" :class="{ active: direction === 'LONG' }" @click="setDirection('LONG')">LONG · 向上</button><button type="button" class="short" :class="{ active: direction === 'SHORT' }" @click="setDirection('SHORT')">SHORT · 向下</button></div></div>
          <div class="form-grid">
            <div class="field"><label for="notional">名义仓位 USDT</label><input id="notional" v-model.number="notional" type="number" min="1" @change="saveWorkspace" /></div>
            <div class="field"><label for="leverage">杠杆</label><input id="leverage" v-model.number="leverage" type="number" min="1" max="125" @change="saveWorkspace" /></div>
            <div class="field full"><label for="runMode">运行模式</label><select id="runMode" v-model="runMode" @change="saveWorkspace"><option value="simulate">模拟模式</option><option value="live">实盘模式</option></select></div>
          </div>
          <div class="form-status" :class="formErrors.length ? 'error' : 'ok'">{{ formErrors.length ? formErrors.join("\n") : `入场：${lineLabel(lineById(entryLineId))}\n${stopLineId ? `止损：${lineLabel(lineById(stopLineId))}` : "未设置止损，入场后需手动平仓"}` }}</div>
          <div class="button-row"><button class="action-button primary" type="button" :disabled="!canArm" @click="openConfirm">确认并启用策略</button><button v-if="currentStrategy && !terminalStatuses.has(currentStrategy.status)" class="action-button danger" type="button" @click="cancelStrategy">取消策略</button></div>
        </section>

        <section v-if="currentStrategy" class="side-section">
          <div class="section-head"><div><h2>策略监控</h2><p>ID {{ currentStrategy.strategy_id?.slice(0, 12) }}…</p></div><span class="badge">{{ currentStrategy.status?.toUpperCase() }}</span></div>
          <div class="breakout-box">当前价格 {{ formatPrice(currentStrategy.current_price) }}<br />入场线 {{ formatPrice(currentStrategy.entry_line_price) }}<br />{{ currentStrategy.stop_line ? `止损线 ${formatPrice(currentStrategy.stop_line_price)}` : "未设置自动止损" }}</div>
          <div class="events"><div v-for="event in [...(currentStrategy.events || [])].reverse()" :key="`${event.ts}-${event.message}`" class="event"><time>{{ formatTs(event.ts) }}</time>{{ event.message }}</div></div>
        </section>
      </aside>
    </main>

    <dialog ref="confirmDialog">
      <div class="dialog-body">
        <h2>确认启用策略</h2>
        <p>{{ direction }} {{ symbol }} · {{ lineLabel(lineById(entryLineId)) }} · {{ notional }} USDT · {{ leverage }}×。</p>
        <div class="dialog-actions"><button class="action-button" type="button" @click="confirmDialog.close()">返回修改</button><button class="action-button primary" type="button" @click="armStrategy">{{ runMode === "live" ? "确认实盘启用" : "启用模拟策略" }}</button></div>
      </div>
    </dialog>
  </div>
</template>
