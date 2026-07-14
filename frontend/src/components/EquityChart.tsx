import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export function EquityChart(props: { curve: [string, number][]; height?: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#5b6474",
        fontSize: 10,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(31,39,51,0.5)" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      handleScroll: false,
      handleScale: false,
      autoSize: false,
    });
    const series = chart.addAreaSeries({
      lineColor: "#4f8cff",
      topColor: "rgba(79, 140, 255, 0.25)",
      bottomColor: "rgba(79, 140, 255, 0.0)",
      lineWidth: 2,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;

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

  useEffect(() => {
    if (!seriesRef.current) return;
    const seen = new Set<number>();
    const data = [];
    for (const [ts, eq] of props.curve) {
      const t = Math.floor(new Date(ts).getTime() / 1000);
      if (seen.has(t)) continue;
      seen.add(t);
      data.push({ time: t as UTCTimestamp, value: eq });
    }
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [props.curve]);

  return <div ref={containerRef} style={{ height: props.height ?? 110, flexShrink: 0 }} />;
}
