from dataclasses import dataclass
from typing import Literal
import pandas as pd
from datetime import UTC, datetime, timedelta
from app.core.logging import applog

@dataclass
class BenchmarkTrade:
    id: int
    date: str
    sym: str
    dir: Literal["long", "short"]
    entry_et: str
    exit_et: str
    entry_px: float
    exit_px: float
    setup_family: list[str]

BENCHMARK_TRADES = [
    # Jul 6
    dict(id=1,  date="2026-07-06", sym="AMD",  dir="long",  entry_et="09:45", exit_et="10:10", entry_px=552.76, exit_px=568.74, setup_family=["orb","momentum"]),
    dict(id=2,  date="2026-07-06", sym="TSLA", dir="long",  entry_et="09:45", exit_et="10:40", entry_px=398.21, exit_px=404.50, setup_family=["orb","momentum"]),
    dict(id=3,  date="2026-07-06", sym="AAPL", dir="long",  entry_et="10:15", exit_et="10:40", entry_px=309.89, exit_px=311.40, setup_family=["momentum","vwap_reversion"]),
    dict(id=4,  date="2026-07-06", sym="NVDA", dir="long",  entry_et="10:00", exit_et="10:12", entry_px=196.31, exit_px=197.20, setup_family=["momentum","orb"]),
    dict(id=5,  date="2026-07-06", sym="TSLA", dir="long",  entry_et="10:45", exit_et="11:25", entry_px=404.21, exit_px=411.38, setup_family=["momentum"]),
    dict(id=6,  date="2026-07-06", sym="AAPL", dir="long",  entry_et="10:45", exit_et="11:25", entry_px=311.40, exit_px=313.58, setup_family=["momentum"]),
    dict(id=7,  date="2026-07-06", sym="META", dir="long",  entry_et="11:00", exit_et="11:40", entry_px=588.49, exit_px=592.37, setup_family=["momentum","vwap_reversion"]),
    dict(id=8,  date="2026-07-06", sym="TSLA", dir="long",  entry_et="11:45", exit_et="12:55", entry_px=412.57, exit_px=418.06, setup_family=["momentum","bb_squeeze"]),
    dict(id=9,  date="2026-07-06", sym="AMD",  dir="short", entry_et="12:30", exit_et="13:40", entry_px=562.52, exit_px=552.44, setup_family=["momentum","bb_squeeze"]),
    dict(id=10, date="2026-07-06", sym="QQQ",  dir="short", entry_et="13:15", exit_et="13:40", entry_px=723.37, exit_px=721.63, setup_family=["orb","momentum","bb_squeeze"]),
    dict(id=11, date="2026-07-06", sym="META", dir="long",  entry_et="14:00", exit_et="14:40", entry_px=594.03, exit_px=600.34, setup_family=["momentum","bb_squeeze"]),
    # Jul 7
    dict(id=12, date="2026-07-07", sym="AMD",  dir="short", entry_et="09:45", exit_et="10:40", entry_px=519.51, exit_px=503.68, setup_family=["orb","momentum"]),
    dict(id=13, date="2026-07-07", sym="QQQ",  dir="short", entry_et="09:45", exit_et="10:40", entry_px=714.06, exit_px=705.35, setup_family=["orb","momentum"]),
    dict(id=14, date="2026-07-07", sym="TSLA", dir="short", entry_et="09:45", exit_et="10:10", entry_px=414.81, exit_px=408.42, setup_family=["orb","momentum"]),
    dict(id=15, date="2026-07-07", sym="META", dir="short", entry_et="10:15", exit_et="11:10", entry_px=612.17, exit_px=604.16, setup_family=["momentum","bb_squeeze"]),
    dict(id=16, date="2026-07-07", sym="TSLA", dir="short", entry_et="11:15", exit_et="11:40", entry_px=408.29, exit_px=406.58, setup_family=["momentum"]),
    dict(id=17, date="2026-07-07", sym="AMD",  dir="long",  entry_et="11:30", exit_et="12:25", entry_px=511.41, exit_px=524.44, setup_family=["momentum","vwap_reversion","rsi2_reversion"]),
    dict(id=18, date="2026-07-07", sym="NVDA", dir="long",  entry_et="11:30", exit_et="12:25", entry_px=193.05, exit_px=197.42, setup_family=["momentum","vwap_reversion"]),
    dict(id=19, date="2026-07-07", sym="QQQ",  dir="long",  entry_et="11:45", exit_et="12:25", entry_px=707.73, exit_px=712.85, setup_family=["momentum","vwap_reversion"]),
    dict(id=20, date="2026-07-07", sym="META", dir="long",  entry_et="14:00", exit_et="14:25", entry_px=610.20, exit_px=622.48, setup_family=["momentum","bb_squeeze"]),
    dict(id=21, date="2026-07-07", sym="QQQ",  dir="short", entry_et="14:30", exit_et="15:10", entry_px=712.42, exit_px=707.97, setup_family=["momentum","bb_squeeze"]),
    dict(id=22, date="2026-07-07", sym="TSLA", dir="short", entry_et="14:30", exit_et="15:10", entry_px=406.53, exit_px=402.59, setup_family=["momentum"]),
    dict(id=23, date="2026-07-07", sym="AMD",  dir="short", entry_et="14:30", exit_et="15:25", entry_px=522.01, exit_px=510.72, setup_family=["momentum","bb_squeeze"]),
    dict(id=24, date="2026-07-07", sym="MSFT", dir="short", entry_et="14:15", exit_et="15:40", entry_px=392.65, exit_px=388.74, setup_family=["bb_squeeze","vwap_reversion"]),
    dict(id=25, date="2026-07-07", sym="AAPL", dir="short", entry_et="14:45", exit_et="15:40", entry_px=313.05, exit_px=310.69, setup_family=["momentum","bb_squeeze"]),
]


ET_OFFSET = timedelta(hours=4)
CANDIDATE_WINDOW_MIN = 90

def _et_to_utc(date_str: str, time_str: str) -> datetime:
    d = [int(x) for x in date_str.split("-")]
    t = [int(x) for x in time_str.split(":")]
    return datetime(d[0], d[1], d[2], t[0], t[1], tzinfo=UTC) + ET_OFFSET

def _utc_to_et_str(ts: datetime) -> str:
    return (ts - ET_OFFSET).strftime("%H:%M")

def _mfe_remaining(df: pd.DataFrame, from_ts: datetime, to_ts: datetime, direction: str, ref_px: float) -> str:
    if from_ts >= to_ts:
        return "CANDIDATE_AFTER_BENCHMARK_EXIT"
    window = df.loc[from_ts:to_ts]
    if window.empty:
        return "CANDIDATE_AFTER_BENCHMARK_EXIT"
    if direction == "long":
        best = window["high"].max()
        mfe = best - ref_px
    else:
        best = window["low"].min()
        mfe = ref_px - best
    pct = mfe / ref_px * 100.0
    return f"${mfe:.2f} ({pct:.2f}%)"

def _fwd_mfe(df: pd.DataFrame, from_ts: datetime, direction: str, ref_px: float, minutes: int) -> str:
    to_ts = from_ts + timedelta(minutes=minutes)
    window = df.loc[from_ts:to_ts]
    if window.empty:
        return "N/A"
    if direction == "long":
        best = window["high"].max()
        mfe = best - ref_px
    else:
        best = window["low"].min()
        mfe = ref_px - best
    pct = mfe / ref_px * 100.0
    return f"${mfe:.2f} ({pct:.2f}%)"

def _move_consumed(benchmark_px: float, cand_px: float, direction: str) -> str:
    if direction == "long":
        delta = cand_px - benchmark_px
    else:
        delta = benchmark_px - cand_px
    pct = delta / benchmark_px * 100.0
    return f"${delta:+.2f} ({pct:+.2f}%)"


def run_benchmark_attribution(result, bars_dict: dict[str, pd.DataFrame]):
    candidates = []
    for s in result.shadow_signals:
        ts = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
        candidates.append({
            "ts": ts,
            "symbol": s["symbol"],
            "strategy": s["strategy"],
            "direction": s["direction"],
            "signal_price": s["signal_price"],
            "ml_confidence": s.get("ml_confidence"),
            "final_decision": s.get("final_decision"),
            "rejection_stage": s.get("rejection_stage", "none"),
            "rejection_reason": s.get("rejection_reason", ""),
            "_assigned_to": None,
        })
    candidates.sort(key=lambda c: c["ts"])

    benchmarks_sorted = sorted(BENCHMARK_TRADES, key=lambda t: (_et_to_utc(t["date"], t["entry_et"])))
    candidate_assignment = {}

    for bm in benchmarks_sorted:
        entry_utc = _et_to_utc(bm["date"], bm["entry_et"])
        window_end = entry_utc + timedelta(minutes=CANDIDATE_WINDOW_MIN)

        matched = None
        for c in candidates:
            if c["_assigned_to"] is not None:
                continue
            if c["symbol"].upper() != bm["sym"].upper():
                continue
            if c["direction"] != bm["dir"]:
                continue
            if c["ts"] < entry_utc:
                continue
            if c["ts"] > window_end:
                continue
            if c["strategy"] not in bm["setup_family"]:
                continue
            matched = c
            break

        candidate_assignment[bm["id"]] = matched
        if matched is not None:
            matched["_assigned_to"] = bm["id"]

    def find_trade_for_candidate(cand: dict):
        if cand is None or cand.get("final_decision") != "accepted":
            return None
        for tr in result.trades:
            side = "long" if tr.side in ("long", "BUY", "ENTER_LONG") else "short"
            if tr.symbol.upper() != cand["symbol"].upper():
                continue
            if side != cand["direction"]:
                continue
            diff = abs((tr.entry_at - cand["ts"]).total_seconds())
            if diff <= 300:
                return tr
        return None

    applog.info("backtest_audit", "================ 25-TRADE BENCHMARK AUDIT ================")
    for bm in BENCHMARK_TRADES:
        entry_utc = _et_to_utc(bm["date"], bm["entry_et"])
        exit_utc = _et_to_utc(bm["date"], bm["exit_et"])
        cand = candidate_assignment[bm["id"]]
        df = bars_dict.get(bm["sym"].upper(), pd.DataFrame())

        if cand is None:
            first_cand_et = "None"
            delay_str = "N/A"
            move_consumed_str = "N/A"
            bm_window_mfe_str = "N/A"
            fwd_mfe_15_str = "N/A"
            fwd_mfe_30_str = "N/A"
            fwd_mfe_60_str = "N/A"
            ml_prob_str = "N/A"
            rej_stage = "NO_CANDIDATE"
            rej_reason = "No strategy trigger in window"
            classification = "NO_CANDIDATE"
        else:
            c_ts = cand["ts"]
            first_cand_et = _utc_to_et_str(c_ts)
            delay_mins = (c_ts - entry_utc).total_seconds() / 60.0
            delay_str = f"+{delay_mins:.0f} min"
            cand_px = cand["signal_price"]
            move_consumed_str = _move_consumed(bm["entry_px"], cand_px, bm["dir"])
            bm_window_mfe_str = _mfe_remaining(df, c_ts, exit_utc, bm["dir"], cand_px)
            fwd_mfe_15_str = _fwd_mfe(df, c_ts, bm["dir"], cand_px, 15)
            fwd_mfe_30_str = _fwd_mfe(df, c_ts, bm["dir"], cand_px, 30)
            fwd_mfe_60_str = _fwd_mfe(df, c_ts, bm["dir"], cand_px, 60)

            ml = cand["ml_confidence"]
            ml_prob_str = f"{ml:.6f}" if ml is not None else "N/A"
            rej_stage = cand["rejection_stage"] or "none"
            rej_reason = cand["rejection_reason"] or ""

            if c_ts > exit_utc:
                classification = "CANDIDATE_AFTER_BENCHMARK_EXIT"
            elif delay_mins > 30:
                classification = "LATE_CANDIDATE"
            elif cand["strategy"] not in bm["setup_family"]:
                classification = "WRONG_STRATEGY_PHASE"
            elif cand["final_decision"] == "accepted":
                classification = "MATCHED_CANDIDATE"
            else:
                classification = "MATCHED_CANDIDATE"

        act_trade = find_trade_for_candidate(cand)
        if act_trade is not None:
            act_entry_et = _utc_to_et_str(act_trade.entry_at)
            act_px_str = f"${act_trade.entry_price:.2f}"
            slip = act_trade.entry_price - bm["entry_px"] if bm["dir"] == "long" else bm["entry_px"] - act_trade.entry_price
            slip_pct = slip / bm["entry_px"] * 100.0
            slippage_str = f"${slip:+.2f} ({slip_pct:+.2f}%)"
            pnl_str = f"{act_trade.pnl_pct:+.2f}% (${act_trade.pnl:+.2f})"
        else:
            act_entry_et = "None"
            act_px_str = "N/A"
            slippage_str = "N/A"
            pnl_str = "N/A"

        print(
            f"ID={bm['id']:02d} {bm['sym']} {bm['dir'].upper():5s} | BM: {bm['entry_et']}-{bm['exit_et']} @ {bm['entry_px']:.2f} | "
            f"CLASS: {classification:30s} | Cand: {first_cand_et} delay={delay_str} move_cons={move_consumed_str} "
            f"| ML: {ml_prob_str} Rej: {rej_stage} | Trade: {act_entry_et} {act_px_str} pnl={pnl_str}"
        )
