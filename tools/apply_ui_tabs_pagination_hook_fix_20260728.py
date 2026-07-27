from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
bottom = ROOT / "frontend/src/components/BottomTabs.tsx"
fmt = ROOT / "frontend/src/format.ts"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"anchor not found for {label}")
    return text.replace(old, new, 1)


src = bottom.read_text(encoding="utf-8")

# React #310: never call a hook conditionally from JSX.
src = replace_once(
    src,
    'const positions=useStore(s=>s.positions),trades=useStore(s=>s.trades),logs=useStore(s=>s.logs);',
    'const positions=useStore(s=>s.positions),orders=useStore(s=>s.orders),trades=useStore(s=>s.trades),logs=useStore(s=>s.logs);',
    "stable orders subscription",
)
src = replace_once(
    src,
    'Orders {useStore(s=>s.orders).length>0&&`(${useStore(s=>s.orders).length})`}',
    'Orders {orders.length>0&&`(${orders.length})`}',
    "remove conditional store hook",
)

# Orders pagination: keep the full durable count in the tab, render only one page.
old_orders = 'function OrdersTable(){const orders=useStore(s=>s.orders),status=useStore(s=>s.status),showToast=useStore(s=>s.showToast);const[symbol,setSymbol]=useState(""),[side,setSide]=useState<"buy"|"sell">("buy"),[qty,setQty]=useState("10");'
new_orders = 'function OrdersTable(){const orders=useStore(s=>s.orders),status=useStore(s=>s.status),showToast=useStore(s=>s.showToast);const[symbol,setSymbol]=useState(""),[side,setSide]=useState<"buy"|"sell">("buy"),[qty,setQty]=useState("10"),[page,setPage]=useState(0);const pageSize=25,pageCount=Math.max(1,Math.ceil(orders.length/pageSize)),safePage=Math.min(page,pageCount-1),visibleOrders=orders.slice(safePage*pageSize,(safePage+1)*pageSize);useEffect(()=>{if(page>=pageCount)setPage(pageCount-1)},[page,pageCount]);'
src = replace_once(src, old_orders, new_orders, "orders pagination state")
src = replace_once(src, '<tbody>{orders.map(o=>', '<tbody>{visibleOrders.map(o=>', "orders visible page")
src = replace_once(
    src,
    '</tbody></table>}</div>}\nfunction TradesTable()',
    '</tbody></table>}{orders.length>pageSize&&<Pagination page={safePage} pageCount={pageCount} total={orders.length} noun="orders" onPage={setPage}/>}</div>}\nfunction TradesTable()',
    "orders pagination controls",
)

# Trades pagination and explicit market Date + Time columns.
old_trades = 'function TradesTable(){const trades=useStore(s=>s.trades);if(!trades.length)return <div className="empty-state">No closed trades yet</div>;return <table className="grid"><thead><tr><th>Closed</th>'
new_trades = 'function TradesTable(){const trades=useStore(s=>s.trades),[page,setPage]=useState(0);const pageSize=25,pageCount=Math.max(1,Math.ceil(trades.length/pageSize)),safePage=Math.min(page,pageCount-1),visibleTrades=trades.slice(safePage*pageSize,(safePage+1)*pageSize);useEffect(()=>{if(page>=pageCount)setPage(pageCount-1)},[page,pageCount]);if(!trades.length)return <div className="empty-state">No closed trades yet</div>;return <div><table className="grid"><thead><tr><th>Date (ET)</th><th>Time (ET)</th>'
src = replace_once(src, old_trades, new_trades, "trades pagination and headers")
src = replace_once(src, '<tbody>{trades.map(t=><tr key={t.id}><td className="num faint">{fmtDateTime(t.exit_at)}</td>', '<tbody>{visibleTrades.map(t=><tr key={t.id}><td className="num faint">{fmtDate(t.exit_at)}</td><td className="num faint">{fmtTime(t.exit_at)}</td>', "trade date/time cells")
src = replace_once(
    src,
    '</tbody></table>}\ntype PercentEvidence=',
    '</tbody></table>{trades.length>pageSize&&<Pagination page={safePage} pageCount={pageCount} total={trades.length} noun="trades" onPage={setPage}/>}</div>}\nfunction Pagination({page,pageCount,total,noun,onPage}:{page:number;pageCount:number;total:number;noun:string;onPage:(page:number)=>void}){return <div className="row" style={{justifyContent:"flex-end",gap:8,padding:"8px 10px"}}><span className="faint num">{page*25+1}-{Math.min((page+1)*25,total)} of {total} {noun}</span><button className="btn sm" disabled={page<=0} onClick={()=>onPage(page-1)}>Previous</button><span className="faint num">Page {page+1} / {pageCount}</span><button className="btn sm" disabled={page>=pageCount-1} onClick={()=>onPage(page+1)}>Next</button></div>}\ntype PercentEvidence=',
    "trade pagination controls",
)

# BottomTabs now uses fmtDate for explicit date column.
src = replace_once(src, 'fmtDateTime, fmtMoney', 'fmtDate, fmtDateTime, fmtMoney', "fmtDate import")
bottom.write_text(src, encoding="utf-8")

fmt_src = fmt.read_text(encoding="utf-8")
anchor = 'export const fmtDateTime = (iso: string | null | undefined): string => {'
fmt_date = '''export const fmtDate = (iso: string | null | undefined): string => {\n  if (!iso) return "—";\n  const d = new Date(iso);\n  return d.toLocaleDateString("en-US", {\n    timeZone: MARKET_TIME_ZONE,\n    year: "numeric",\n    month: "short",\n    day: "2-digit",\n  });\n};\n\n'''
if 'export const fmtDate =' not in fmt_src:
    fmt_src = replace_once(fmt_src, anchor, fmt_date + anchor, "market date formatter")
fmt.write_text(fmt_src, encoding="utf-8")

print("Applied hook-stability, Orders/Trades pagination, and explicit ET trade date/time fix.")
print("Changed: frontend/src/components/BottomTabs.tsx, frontend/src/format.ts")