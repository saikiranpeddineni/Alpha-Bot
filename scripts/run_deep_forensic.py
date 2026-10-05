import sqlite3
import json
import datetime
import pandas as pd
import numpy as np

def parse_iso(dt_str):
    if not dt_str:
        return None
    try:
        dt_str = dt_str.strip()
        if dt_str.endswith('Z'):
            dt_str = dt_str[:-1] + '+00:00'
        # If no timezone specified, assume UTC
        dt = datetime.datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except Exception as e:
        return None

def run_deep_forensic():
    conn_alpha = sqlite3.connect('data/alphadesk.db')
    cur = conn_alpha.cursor()
    
    dates = [r[0] for r in cur.execute("SELECT DISTINCT substr(exit_at, 1, 10) as s_date FROM trades ORDER BY s_date DESC LIMIT 10").fetchall()]
    dates.reverse() # chronological
    print(f"Analyzing {len(dates)} sessions: {dates[0]} to {dates[-1]}")
    
    placeholders = ','.join(['?']*len(dates))
    query = f"""
        SELECT id, exit_at, symbol, pnl, mode, payload 
        FROM trades 
        WHERE substr(exit_at, 1, 10) IN ({placeholders}) 
        ORDER BY exit_at ASC
    """
    rows = cur.execute(query, dates).fetchall()
    print(f"Retrieved {len(rows)} trades.")
    
    conn_bars = sqlite3.connect('data/bars_cache.db')
    cur_bars = conn_bars.cursor()
    
    conn_market = sqlite3.connect('data/market.db')
    cur_market = conn_market.cursor()
    
    trades = []
    
    for r in rows:
        tid, exit_at, sym, pnl, mode, pl_str = r
        pl = json.loads(pl_str) if pl_str else {}
        
        entry_at = pl.get('entry_at')
        entry_price = float(pl.get('entry_price', 0.0))
        exit_price = float(pl.get('exit_price', 0.0))
        side = pl.get('side', 'long').lower()
        qty = float(pl.get('qty', 0.0))
        initial_stop = float(pl.get('initial_stop', 0.0)) if pl.get('initial_stop') else 0.0
        
        # Calculate initial risk per share
        if initial_stop > 0 and entry_price > 0:
            initial_risk = abs(entry_price - initial_stop)
        else:
            initial_risk = float(pl.get('initial_risk', 0.0)) if pl.get('initial_risk') else 0.0
            
        strategy = pl.get('strategy', 'unknown')
        exit_reason = pl.get('exit_reason', '')
        r_multiple = float(pl.get('r_multiple', 0.0))
        
        entry_epoch = parse_iso(entry_at)
        exit_epoch = parse_iso(exit_at)
        
        # If exit_at in table doesn't have T, it might be '2026-10-05 15:33:16.247245'
        if not exit_epoch:
            exit_epoch = parse_iso(pl.get('exit_at'))
            
        trade_bars = []
        post_bars = []
        
        if entry_epoch and exit_epoch:
            # Query trade bars from bars_cache.db
            b_rows = cur_bars.execute(
                """SELECT ts, open, high, low, close, volume 
                   FROM bars 
                   WHERE symbol = ? AND tf IN ('1Min', 'yahoo:1Min') AND ts >= ? AND ts <= ? 
                   ORDER BY ts ASC""",
                (sym, entry_epoch - 60, exit_epoch + 1800)
            ).fetchall()
            
            if not b_rows and conn_market:
                b_rows = cur_market.execute(
                    """SELECT ts, open, high, low, close, volume 
                       FROM bars 
                       WHERE symbol = ? AND tf IN ('1Min', 'yahoo:1Min') AND ts >= ? AND ts <= ? 
                       ORDER BY ts ASC""",
                    (sym, entry_epoch - 60, exit_epoch + 1800)
                ).fetchall()
            
            trade_bars = [b for b in b_rows if entry_epoch - 30 <= b[0] <= exit_epoch + 30]
            post_bars = [b for b in b_rows if b[0] > exit_epoch + 30]
        
        # Analyze 1-minute bars during trade
        mfe_price = entry_price
        mae_price = entry_price
        highest_price = entry_price
        lowest_price = entry_price
        
        bar_count = len(trade_bars)
        
        for b in trade_bars:
            ts, o, h, l, c, v = b
            if h > highest_price:
                highest_price = h
            if l < lowest_price:
                lowest_price = l
                
        is_long = (side in ('long', 'buy'))
        if is_long:
            mfe_price = highest_price
            mae_price = lowest_price
            mfe_dollars = (mfe_price - entry_price) * qty
            mae_dollars = (entry_price - mae_price) * qty
            mfe_r = (mfe_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
            mae_r = (entry_price - mae_price) / initial_risk if initial_risk > 0 else 0.0
        else:
            mfe_price = lowest_price
            mae_price = highest_price
            mfe_dollars = (entry_price - mfe_price) * qty
            mae_dollars = (mae_price - entry_price) * qty
            mfe_r = (entry_price - mfe_price) / initial_risk if initial_risk > 0 else 0.0
            mae_r = (mae_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
            
        # Post-exit excursion (next 30 mins)
        post_mfe_price = mfe_price
        for b in post_bars:
            ts, o, h, l, c, v = b
            if is_long:
                if h > post_mfe_price:
                    post_mfe_price = h
            else:
                if l < post_mfe_price:
                    post_mfe_price = l
                    
        if is_long:
            post_mfe_r = (post_mfe_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
        else:
            post_mfe_r = (entry_price - post_mfe_price) / initial_risk if initial_risk > 0 else 0.0
            
        left_on_table_r = max(0.0, post_mfe_r - max(0.0, r_multiple))
        
        # Did the trade hit green before losing?
        went_green = (mfe_r >= 0.20)
        dead_on_arrival = (mfe_r < 0.20)
        
        trades.append({
            'id': tid,
            'session': exit_at[:10],
            'symbol': sym,
            'strategy': strategy,
            'side': side,
            'qty': qty,
            'entry_at': entry_at,
            'exit_at': exit_at,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'initial_stop': initial_stop,
            'initial_risk': initial_risk,
            'pnl': pnl,
            'r_multiple': r_multiple,
            'bar_count': bar_count,
            'calc_mfe_r': mfe_r,
            'calc_mae_r': mae_r,
            'post_30m_mfe_r': post_mfe_r,
            'left_on_table_r': left_on_table_r,
            'went_green': went_green,
            'dead_on_arrival': dead_on_arrival,
            'exit_reason': exit_reason
        })
        
    df = pd.DataFrame(trades)
    
    print("\n=======================================================")
    print("      DEEP 1-MINUTE BAR FORENSIC AUDIT (LAST 10 SESSIONS)    ")
    print("=======================================================")
    print(f"Total Trades: {len(df)}")
    print(f"Total PnL: ${df['pnl'].sum():.2f}")
    print(f"Total Realized R: {df['r_multiple'].sum():.2f}R")
    print(f"Win Rate: {(df['pnl'] > 0).mean()*100:.1f}% ({len(df[df['pnl'] > 0])}W / {len(df[df['pnl'] < 0])}L / {len(df[df['pnl'] == 0])}BE)")
    
    gross_win = df[df['pnl'] > 0]['pnl'].sum()
    gross_loss = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else 999.0
    print(f"Profit Factor: {pf:.3f} | Gross Win: ${gross_win:.2f} | Gross Loss: ${gross_loss:.2f}")
    print(f"Avg Bars in Trade: {df['bar_count'].mean():.1f} mins (Median: {df['bar_count'].median():.1f} mins)")
    print(f"Trades with 1m bar data matched: {len(df[df['bar_count'] > 0])} / {len(df)}")
    
    print("\n-------------------------------------------------------")
    print("1. DEAD-ON-ARRIVAL (DOA) VS EXCURSION BREAKDOWN")
    print("-------------------------------------------------------")
    doa_trades = df[df['dead_on_arrival']]
    green_trades = df[~df['dead_on_arrival']]
    print(f"Dead on Arrival (MFE < 0.20R): {len(doa_trades)} trades ({len(doa_trades)/len(df)*100:.1f}%) | PnL: ${doa_trades['pnl'].sum():.2f} | Realized R: {doa_trades['r_multiple'].sum():.2f}R")
    print(f"Saw Green (MFE >= 0.20R):     {len(green_trades)} trades ({len(green_trades)/len(df)*100:.1f}%) | PnL: ${green_trades['pnl'].sum():.2f} | Realized R: {green_trades['r_multiple'].sum():.2f}R")
    
    print("\n-------------------------------------------------------")
    print("2. PEAK R EXCURSION DISTRIBUTION (What did trades reach?)")
    print("-------------------------------------------------------")
    bins = [-np.inf, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0, np.inf]
    labels = ['< 0.2R (DOA)', '0.2R - 0.5R', '0.5R - 1.0R', '1.0R - 1.5R', '1.5R - 2.0R', '2.0R - 3.0R', '>= 3.0R (Runners)']
    df['peak_bucket'] = pd.cut(df['calc_mfe_r'], bins=bins, labels=labels)
    
    bucket_grp = df.groupby('peak_bucket', observed=False).agg(
        trades=('id', 'count'),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        realized_r=('r_multiple', 'sum'),
        avg_realized_r=('r_multiple', 'mean'),
        avg_mfe_r=('calc_mfe_r', 'mean'),
        avg_post_30m_mfe=('post_30m_mfe_r', 'mean'),
        win_rate=('pnl', lambda x: f"{(x > 0).mean()*100:.1f}%")
    )
    print(bucket_grp.to_string())
    
    print("\n-------------------------------------------------------")
    print("3. STRATEGY ATTRIBUTION FORENSICS")
    print("-------------------------------------------------------")
    strat_grp = df.groupby('strategy').agg(
        trades=('id', 'count'),
        win_rate=('pnl', lambda x: f"{(x > 0).mean()*100:.1f}%"),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        realized_r=('r_multiple', 'sum'),
        avg_realized_r=('r_multiple', 'mean'),
        avg_mfe_r=('calc_mfe_r', 'mean'),
        avg_mae_r=('calc_mae_r', 'mean'),
        avg_post_mfe_r=('post_30m_mfe_r', 'mean'),
        doa_rate=('dead_on_arrival', lambda x: f"{x.mean()*100:.1f}%"),
        avg_hold_mins=('bar_count', 'mean')
    )
    print(strat_grp.to_string())
    
    print("\n-------------------------------------------------------")
    print("4. GIVEOVER FORENSICS: WINNERS THAT GAVE BACK GAINS")
    print("-------------------------------------------------------")
    # Trades that reached >= 1.0R peak but exited <= 0.2R or negative
    gave_back = df[(df['calc_mfe_r'] >= 1.0) & (df['r_multiple'] <= 0.2)]
    print(f"Trades reaching >= 1.0R peak but realizing <= 0.2R: {len(gave_back)}")
    if len(gave_back) > 0:
        print(f"PnL of giveback trades: ${gave_back['pnl'].sum():.2f}")
        print(gave_back[['session', 'symbol', 'strategy', 'pnl', 'r_multiple', 'calc_mfe_r', 'calc_mae_r', 'bar_count', 'exit_reason']].to_string())
        
    print("\n-------------------------------------------------------")
    print("5. POST-EXIT CONTINUATION: MONEY LEFT ON THE TABLE")
    print("-------------------------------------------------------")
    left_behind = df[df['left_on_table_r'] >= 1.0]
    print(f"Trades where price ran >= +1.0R further within 30m after exit: {len(left_behind)} / {len(df)}")
    print(f"Total R left on table in those trades: {left_behind['left_on_table_r'].sum():.2f}R")
    if len(left_behind) > 0:
        print(left_behind[['session', 'symbol', 'strategy', 'pnl', 'r_multiple', 'calc_mfe_r', 'post_30m_mfe_r', 'left_on_table_r', 'exit_reason']].head(10).to_string())

    print("\n-------------------------------------------------------")
    print("6. EXIT REASON BREAKDOWN & PROFITABILITY")
    print("-------------------------------------------------------")
    exit_grp = df.groupby('exit_reason').agg(
        trades=('id', 'count'),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        realized_r=('r_multiple', 'sum'),
        avg_realized_r=('r_multiple', 'mean'),
        avg_mfe_r=('calc_mfe_r', 'mean'),
        avg_hold_mins=('bar_count', 'mean')
    )
    print(exit_grp.sort_values(by='total_pnl').to_string())
    
    # Save enriched df to CSV for record
    df.to_csv('data/forensic_trades_10_sessions_enriched.csv', index=False)
    print("\nSaved enriched forensic dataframe to data/forensic_trades_10_sessions_enriched.csv")

    conn_alpha.close()
    conn_bars.close()

if __name__ == '__main__':
    run_deep_forensic()
