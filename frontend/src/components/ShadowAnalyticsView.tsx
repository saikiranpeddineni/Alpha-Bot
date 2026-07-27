import type { ReactNode } from "react";

export function ShadowSection({title,subtitle,children}:{title:string;subtitle:string;children:ReactNode}){
 return <section style={{background:"var(--panel)",border:"1px solid var(--border)",borderRadius:8,overflow:"hidden"}}><header style={{padding:"9px 12px",borderBottom:"1px solid var(--border)",display:"flex",justifyContent:"space-between",gap:12,alignItems:"center"}}><div><div style={{fontSize:11,fontWeight:700,letterSpacing:".04em"}}>{title}</div><div className="faint" style={{fontSize:9,marginTop:1}}>{subtitle}</div></div></header><div style={{padding:10}}>{children}</div></section>;
}

export function ShadowKpi({label,value,hint,tone=""}:{label:string;value:string|number;hint:string;tone?:string}){
 return <div style={{background:"var(--panel-2)",border:"1px solid var(--border)",borderRadius:7,padding:"10px 11px",minHeight:70}}><div className="faint" style={{fontSize:9,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase"}}>{label}</div><div className={`num ${tone}`} style={{fontSize:20,fontWeight:750,lineHeight:1.25,marginTop:3}}>{value}</div><div className="muted" style={{fontSize:9,marginTop:2}}>{hint}</div></div>;
}

export function AlphaBar({value,max}:{value:number;max:number}){
 const width=max>0?Math.min(100,Math.abs(value)/max*100):0;
 return <div style={{height:4,background:"var(--border)",borderRadius:4,overflow:"hidden",marginTop:5}}><div style={{width:`${width}%`,height:"100%",background:value>=0?"var(--green)":"var(--red)"}}/></div>;
}