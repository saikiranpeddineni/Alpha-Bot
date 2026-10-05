import re
from collections import Counter

def analyze_app_log_rejections():
    with open('data/logs/app.log', 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        
    print(f"Total lines in app.log: {len(lines)}")
    
    # Filter for today: 2026-10-05
    today_lines = [l for l in lines if l.startswith('2026-10-05')]
    print(f"Total lines from 2026-10-05: {len(today_lines)}")
    
    rejections = []
    
    for l in today_lines:
        if 'rejected' in l.lower() or 'skipped' in l.lower() or 'blocked' in l.lower() or 'veto' in l.lower():
            # Find gate / reason
            if 'pipeline' in l:
                # e.g.: [pipeline] COHR momentum enter_short: rejected -- Opening blackout: entry suppressed (3min after open)
                m = re.search(r'\[pipeline\]\s+([^:]+):\s+(?:rejected\s+--\s+)?(.*)', l)
                if m:
                    rejections.append(('pipeline', m.group(2).strip()))
            elif 'Behavioral Loop Detector' in l:
                # e.g.: [engine] MU: entry rejected by Behavioral Loop Detector — behavioral_loop_detector: high portfolio churn/turnover (turnover=2.57x equity > 2.00x)
                m = re.search(r'Behavioral Loop Detector\s+[—\-]+\s+(.*)', l)
                if m:
                    rejections.append(('behavioral_loop_detector', m.group(1).strip()))
            elif 'Portfolio Gate' in l or 'Allocator rejected' in l:
                m = re.search(r'Portfolio Gate/Allocator rejected\s+[—\-]+\s+(.*)', l)
                if m:
                    rejections.append(('portfolio_gate', m.group(1).strip()))
            elif 'entry evaluation skipped' in l:
                m = re.search(r'entry evaluation skipped\s+[—\-]+\s+(.*)', l)
                if m:
                    rejections.append(('engine_skip', m.group(1).strip()))
            elif 'Market-data gap detected' in l:
                rejections.append(('market_data_gap', 'Market-data gap detected'))
                
    counts = Counter(rejections)
    print("\n--- ALL REJECTIONS ON 2026-10-05 BY CATEGORY & REASON ---")
    for (cat, reason), cnt in counts.most_common(50):
        print(f"Count: {cnt:3d} | [{cat:22s}] {reason}")

if __name__ == '__main__':
    analyze_app_log_rejections()
