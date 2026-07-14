import {
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { fmtPct, pnlClass } from "../format";
import { useStore } from "../store";
import type { Bar, TradeRow } from "../types";

const TIMEFRAMES: { key: string; label: string; seconds: number }[] = [
  { key: "1Min", label: "1m", seconds: 60 },
  { key: "5Min", label: "5m", seconds: 300 },
  { key: "15Min", label: "15m", seconds: 900 },
];

function etSessionKey(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleDateString("en-US", { timeZone: "America/New_York" });
}

function computeOverlays(bars: Bar[]): { ema: LineData[]; vwap: LineData[] } {
  const ema: LineData[] = [];
  const vwap: LineData[] = [];
  const k = 2 / (20 + 1);
  let emaVal: number | null = null;
  let session = "";
  let pv = 0;
  let vv = 0;
  for (const b of bars) {
    emaVal = emaVal == null ? b.c : b.c * k + emaVal * (1 - k);
    ema.push({ time: b.t as UTCTimestamp, value: emaVal });
    const key = etSessionKey(b.t);
    if (key !== session) {
      session = key;
      pv = 0;
      vv = 0;
    }
    const typical = (b.h + b.l + b.c) / 3;
    pv += typical * b.v;
    vv += b.v;
    vwap.push({ time: b.t as UTCTimestamp, value: vv > 0 ? pv / vv : b.c });
  }
  return { ema, vwap };
}

function toMarkers(trades: TradeRow[], symbol: string, firstBarTime: number, tfSec: number): SeriesMarker<UTCTimestamp>[] {
  const bucket = (iso: string) => (Math.floor(new Date(iso).getTime() / 1000 / tfSec) * tfSec) as UTCTimestamp;
  const markers: SeriesMarker<UTCTimestamp>[] = [];
  for (const t of trades) {
    if (t.symbol !== symbol) continue;
    const entryTime = bucket(t.entry_at);
    const exitTime = bucket(t.exit_at);
    if (entryTime >= firstBarTime) {
      markers.push({
        time: entryTime,
        position: t.side === "long" ? "belowBar" : "aboveBar",
        color: t.side === "long" ? "#22c55e" : "#ef4444",
        shape: t.side === "long" ? "arrowUp" : "arrowDown",
        text: `${t.side === "long" ? "B" : "S"} ${t.qty}`,
      });
    }
    if (exitTime >= firstBarTime) {
      markers.push({
        time: exitTime,
        position: t.side === "long" ? "aboveBar" : "belowBar",
        color: t.pnl >= 0 ? "#22c55e" : "#ef4444",
        shape: "circle",
        text: `${t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(0)}`,
      });
    }
  }
  return markers.sort((a, b) => (a.time as number) - (b.time as number));
}

export function PriceChart() {
  const symbol = useStore((s) => s.selectedSymbol);
  const liveBar = useStore((s) => s.liveBar);
  const trades = useStore((s) => s.trades);
  const quotes = useStore((s) => s.quotes);
  const [tf, setTf] = useState(TIMEFRAMES[0]);
  const [loading, setLoading] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const emaRef = useRef<ISeriesApi<"Line"> | null>(null);
  const vwapRef = useRef<ISeriesApi<"Line"> | null>(null);
  const barsRef = useRef<Bar[]>([]);

  const quote = useMemo(() => quotes.find((q) => q.symbol === symbol), [quotes, symbol]);

  // ---- create chart once ----
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b95a8",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(31, 39, 51, 0.6)" },
        horzLines: { color: "rgba(31, 39, 51, 0.6)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1f2733" },
      timeScale: { borderColor: "#1f2733", timeVisible: true, secondsVisible: false },
      autoSize: false,
    });
    const candles = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e88",
      wickDownColor: "#ef444488",
    });
    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const ema = chart.addLineSeries({
      color: "#4f8cff",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const vwap = chart.addLineSeries({
      color: "#f59e0b",
      lineWidth: 1,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    chartRef.current = chart;
    candlesRef.current = candles;
    volumeRef.current = volume;
    emaRef.current = ema;
    vwapRef.current = vwap;

    const resize = () => chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  function render(bars: Bar[]) {
    if (!candlesRef.current || !volumeRef.current) return;
    barsRef.current = bars;
    candlesRef.current.setData(
      bars.map<CandlestickData>((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })),
    );
    volumeRef.current.setData(
      bars.map<HistogramData>((b) => ({
        time: b.t as UTCTimestamp,
        value: b.v,
        color: b.c >= b.o ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
      })),
    );
    const overlays = computeOverlays(bars);
    emaRef.current?.setData(overlays.ema);
    vwapRef.current?.setData(overlays.vwap);
    if (bars.length) {
      candlesRef.current.setMarkers(toMarkers(trades, symbol, bars[0].t, tf.seconds));
    }
  }

  // ---- (re)load history on symbol / timeframe change ----
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .bars(symbol, tf.key, 500)
      .then((res) => {
        if (cancelled) return;
        render(res.bars);
        chartRef.current?.timeScale().fitContent();
      })
      .catch(() => undefined)
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, tf.key]);

  // ---- markers refresh when trades change ----
  useEffect(() => {
    const bars = barsRef.current;
    if (bars.length && candlesRef.current) {
      candlesRef.current.setMarkers(toMarkers(trades, symbol, bars[0].t, tf.seconds));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trades]);

  // ---- live bar merge (1-min closed bars aggregated into the active tf) ----
  useEffect(() => {
    if (!liveBar || liveBar.symbol !== symbol) return;
    const bars = barsRef.current;
    if (!bars.length || !candlesRef.current || !volumeRef.current) return;
    const b = liveBar.bar;
    const bucketTime = Math.floor(b.t / tf.seconds) * tf.seconds;
    const last = bars[bars.length - 1];

    let merged: Bar;
    if (last.t === bucketTime) {
      merged = { t: bucketTime, o: last.o, h: Math.max(last.h, b.h), l: Math.min(last.l, b.l), c: b.c, v: last.v + b.v };
      bars[bars.length - 1] = merged;
    } else if (bucketTime > last.t) {
      merged = { t: bucketTime, o: b.o, h: b.h, l: b.l, c: b.c, v: b.v };
      bars.push(merged);
      if (bars.length > 600) bars.shift();
    } else {
      return; // stale bar
    }
    candlesRef.current.update({ time: merged.t as UTCTimestamp, open: merged.o, high: merged.h, low: merged.l, close: merged.c });
    volumeRef.current.update({
      time: merged.t as UTCTimestamp,
      value: merged.v,
      color: merged.c >= merged.o ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
    });
    const overlays = computeOverlays(bars);
    if (overlays.ema.length) emaRef.current?.update(overlays.ema[overlays.ema.length - 1]);
    if (overlays.vwap.length) vwapRef.current?.update(overlays.vwap[overlays.vwap.length - 1]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveBar]);

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="chart-head">
          <span className="chart-sym">{symbol}</span>
          {quote && (
            <>
              <span className="chart-price num">{quote.price.toFixed(2)}</span>
              <span className={`num ${pnlClass(quote.change_pct)}`} style={{ fontSize: 12 }}>
                {fmtPct(quote.change_pct)}
              </span>
            </>
          )}
          {loading && <span className="faint">loading…</span>}
        </div>
        <div className="row">
          <span className="faint" style={{ textTransform: "none", letterSpacing: 0 }}>
            <span style={{ color: "#4f8cff" }}>—</span> EMA20&ensp;
            <span style={{ color: "#f59e0b" }}>┄</span> VWAP
          </span>
          <div className="tf-group">
            {TIMEFRAMES.map((t) => (
              <button key={t.key} className={tf.key === t.key ? "active" : ""} onClick={() => setTf(t)}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="chart-wrap">
        <div ref={containerRef} className="chart-el" />
      </div>
    </div>
  );
}
