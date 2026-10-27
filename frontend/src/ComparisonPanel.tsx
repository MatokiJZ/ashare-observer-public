import React, {useEffect, useMemo, useState} from 'react';
import type {EChartsOption} from 'echarts';
import {Check, Download, Loader2, Plus, RefreshCw, Search, X} from 'lucide-react';
import './comparison.css';
import {candleHeight, candleOption, type CandleSeries} from './comparisonCandles';

type Asset = {id:string; kind:string; symbol:string; name:string};
type Series = Asset & CandleSeries & {values:(number|null)[]; prices:(number|null)[]; state:string; points:number; expected_points:number;
  last_date:string|null; last_close:number|null; change_pct:number|null; source?:string; fetched_at?:string;
  adjustment:string; error?:string; refreshing:boolean};
type Pair = {left:string; right:string; value:number|null; samples:number; reason?:string};
type Result = {dates:string[]; series:Series[]; pairs:Pair[]; baseline_date:string|null; expected_through:string;
  generated_at:string; window:number; refreshing:boolean; warnings:string[]; method:string};
const COLORS = ['#2563a6','#dd8a16','#098776','#8b53a6','#ce4942','#557e19','#0c8fbc','#8c5941'];
const TYPES:Record<string,string> = {stock:'股票',sector:'行业板块',index:'指数'};
const DEFAULT_IDS = ['stock:600519','stock:000858','index:sh000300'];
const STORE = 'ashare-comparison-v1';
const format = (v:number|null|undefined, digits=2) => v == null ? '—' : v.toFixed(digits);
const change = (v:number|null|undefined) => v == null ? '—' : `${v>0?'+':''}${format(v)}%`;
const stamp = (v?:string) => v ? new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '未取得';
function saved() {
  try {const value = JSON.parse(localStorage.getItem(STORE)||'null');
    if (Array.isArray(value?.ids) && value.ids.length<=8 && value.ids.every((id:unknown)=>typeof id==='string'))
      return {ids:[...new Set<string>(value.ids)], window:[20,60,120,250].includes(value.window)?value.window:60,view:value.view==='lines'?'lines':'candles'};
  } catch { /* Browser storage may be disabled. */ }
  return {ids:DEFAULT_IDS, window:60,view:'candles'};
}
async function read(path:string, signal?:AbortSignal) {
  const response = await fetch('/api/comparison'+path,{signal});
  if(!response.ok) throw new Error((await response.json()).detail || '对比数据读取失败');
  return response.json();
}

export function ComparisonPanel({Chart}:{Chart:React.ComponentType<{option:EChartsOption;height?:number;preserveZoom?:boolean}>}) {
  const [preference] = useState(saved);
  const [ids,setIds] = useState<string[]>(preference.ids);
  const [windowSize,setWindow] = useState(preference.window);
  const [view,setView] = useState(preference.view);
  const [catalog,setCatalog] = useState<Asset[]>([]);
  const [catalogReady,setCatalogReady] = useState(false);
  const [query,setQuery] = useState('');
  const [kind,setKind] = useState('all');
  const [picker,setPicker] = useState(false);
  const [result,setResult] = useState<Result|null>(null);
  const [error,setError] = useState('');
  const [catalogError,setCatalogError] = useState('');
  const [busy,setBusy] = useState(false);
  const [refresh,setRefresh] = useState(0);
  useEffect(()=>{
    const controller = new AbortController();
    read('/catalog',controller.signal).then(data=>{
      setCatalog(data.items);setCatalogReady(true);setCatalogError('');
      setIds(current=>current.filter(id=>data.items.some((a:Asset)=>a.id===id)));
    }).catch(e=>{if(e.name!=='AbortError')setCatalogError(e.message)});
    return ()=>controller.abort();
  },[refresh]);
  useEffect(()=>{
    try {localStorage.setItem(STORE,JSON.stringify({ids,window:windowSize,view}));} catch { /* Optional persistence. */ }
  },[ids,windowSize,view]);
  const selectionKey = ids.join(',');
  useEffect(()=>{
    setResult(null);setError('');
    if(!catalogReady || !ids.length) {setBusy(false);return;}
    const controller = new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    let first=true;
    const load = async()=>{
      setBusy(true);
      let interval=60000;
      try {
        const params=new URLSearchParams({ids:selectionKey,window:String(windowSize)});
        if(first && refresh) params.set('refresh','true');
        first=false;
        const data = await read('?'+params,controller.signal);
        if(!controller.signal.aborted) {setResult(data);setError('');interval=data.refreshing?2500:60000;}
      } catch(e:any) {if(e.name!=='AbortError')setError(e.message)}
      finally {if(!controller.signal.aborted){setBusy(false);timer=setTimeout(()=>void load(),interval);}}
    };
    void load();
    return ()=>{controller.abort();clearTimeout(timer)};
  },[selectionKey,windowSize,catalogReady,refresh]);
  const byId=useMemo(()=>new Map(catalog.map(a=>[a.id,a])),[catalog]);
  const options=useMemo(()=>{
    const q=query.toLowerCase().replace(/\s/g,'');
    return catalog.filter(a=>(kind==='all'||a.kind===kind) && (!q || (a.name+a.symbol).toLowerCase().replace(/\s/g,'').includes(q)))
      .sort((a,b)=>Number(ids.includes(b.id))-Number(ids.includes(a.id)) || (a.kind==='index'?-1:0)-(b.kind==='index'?-1:0));
  },[catalog,query,kind,selectionKey]);
  const toggle=(id:string)=>setIds(current=>current.includes(id)?current.filter(v=>v!==id):current.length<8?[...current,id]:current);
  const chartOption=useMemo<EChartsOption>(()=>({
    animation:false,color:COLORS,
    grid:{left:58,right:26,top:24,bottom:100},
    tooltip:{trigger:'axis',confine:true,valueFormatter:(v:any)=>v==null?'缺失':change(Number(v))},
    legend:{type:'scroll',bottom:8,itemWidth:22,itemHeight:5,textStyle:{fontSize:12,color:'#4d5e73'}},
    xAxis:{type:'category',data:result?.dates||[],boundaryGap:false,axisLine:{lineStyle:{color:'#dfe5eb'}},
      axisLabel:{color:'#87909e',fontSize:10,formatter:(v:string)=>v.slice(5)},axisPointer:{label:{formatter:(p:any)=>String(p.value)}}},
    yAxis:{type:'value',axisLabel:{color:'#87909e',fontSize:10,formatter:'{value}%'},splitLine:{lineStyle:{color:'#e9edf1',type:'dashed'}}},
    dataZoom:[{type:'inside'},{type:'slider',bottom:38,height:18,borderColor:'#e5eaf0',showDetail:false}],
    series:(result?.series||[]).map((s,i)=>({name:s.name+' · '+s.symbol,type:'line',data:s.values,
      showSymbol:(result?.dates.length||0)<=61,symbolSize:4,connectNulls:false,lineStyle:{width:3.2,type:'solid'},
      emphasis:{focus:'series',lineStyle:{width:4.5}},markLine:i===0?{silent:true,symbol:'none',label:{show:false},data:[{yAxis:0}],lineStyle:{color:'#b7c0cb',width:1,type:'dotted'}}:undefined}))
  }),[result]);
  const candles=useMemo(()=>candleOption(result?.dates||[],result?.series||[]),[result]);
  const exportData=()=>{
    if(!result)return;
    const blob=new Blob([JSON.stringify(result,null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;
    a.download=`走势对比-${result.expected_through}-${windowSize}日.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  const hasLines=result?.series.some(s=>s.points>0);
  return <div className="comparison">
    <section className="panel compare-picker">
      <div className="panel-head"><div><h2>选择对比对象 <span className="compare-count">{ids.length} / 8</span></h2><p>股票、行业板块、指数可以混选；选择保存在当前浏览器。</p></div>
        <div className="compare-actions"><button className="button" onClick={()=>{setIds(DEFAULT_IDS.filter(id=>byId.has(id)));setWindow(60);setQuery('');setKind('all');setPicker(false)}} disabled={!catalogReady}>试用两只股票＋指数</button>
          <button className="button" onClick={()=>setIds([])} disabled={!ids.length}>清空</button></div></div>
      <div className="compare-controls"><div className="segmented" aria-label="对比对象类型">{['all','stock','sector','index'].map(k=><button key={k} className={kind===k?'selected':''} aria-pressed={kind===k} onClick={()=>{setKind(k);setPicker(true)}}>{k==='all'?'全部':TYPES[k]}</button>)}</div>
        <label className="compare-search"><Search size={16}/><input aria-label="搜索股票、板块或指数" placeholder="输入名称或代码，如 茅台 / 银行 / 000300" value={query} onFocus={()=>setPicker(true)} onChange={e=>{setQuery(e.target.value);setPicker(true)}} onKeyDown={e=>{if(e.key==='Escape')setPicker(false)}}/></label>
        <button className="button" onClick={()=>setPicker(!picker)} aria-expanded={picker}>{picker?'收起列表':'浏览列表'}</button></div>
      {catalogError&&<p className="compare-message" role="alert">目录读取失败：{catalogError}，可点击下方“刷新对比”重试。</p>}
      {picker&&<div className="compare-options"><div className="compare-options-head"><span>{catalogReady?`匹配 ${options.length} 项${options.length>18?'，先显示 18 项，请输入名称缩小范围':''}`:'正在读取目录…'}</span><button className="icon-button" aria-label="收起搜索结果" onClick={()=>setPicker(false)}><X size={15}/></button></div>
        <div className="compare-option-grid">{options.slice(0,18).map(a=><button key={a.id} className={'compare-option '+(ids.includes(a.id)?'selected':'')} aria-pressed={ids.includes(a.id)} onClick={()=>toggle(a.id)} disabled={ids.length>=8&&!ids.includes(a.id)} title={a.name+' '+a.symbol}>
          <span><strong>{a.name}</strong><small>{TYPES[a.kind]} · {a.symbol}</small></span>{ids.includes(a.id)?<Check size={16}/>:<Plus size={16}/>}</button>)}</div>
        {catalogReady&&!options.length&&<p className="compare-message">没有匹配项。当前支持沪深 A 股、东方财富行业板块和 6 个主要 A 股指数。</p>}</div>}
      <div className="compare-chips">{ids.map((id,i)=><span className="compare-chip" key={id}><i style={{background:COLORS[i]}}/><span>{byId.get(id)?.name||id}</span><small>{TYPES[byId.get(id)?.kind||'']}</small><button aria-label={'移除'+(byId.get(id)?.name||id)} onClick={()=>toggle(id)}><X size={13}/></button></span>)}
        {!ids.length&&<span className="muted">从上方列表添加对象，即可开始对比。</span>}</div>
    </section>
    <section className="panel">
      <div className="panel-head"><div><h2>{view==='candles'?'K 线与成交额对照':'同一起点，看相对走势'}</h2><p>{view==='candles'?'红涨绿跌 · 各图独立价格轴 · 日期与缩放联动':'已完成日线 · 共同起点 0% · 股票前复权，高对比度实线'}</p></div>
        <div className="segmented" aria-label="对比区间">{[20,60,120,250].map(n=><button key={n} className={windowSize===n?'selected':''} aria-pressed={windowSize===n} onClick={()=>setWindow(n)}>{n} 日</button>)}</div></div>
      <div className="compare-view-toolbar"><div className="segmented" aria-label="图表形式"><button className={view==='candles'?'selected':''} aria-pressed={view==='candles'} onClick={()=>setView('candles')}>K 线对照</button><button className={view==='lines'?'selected':''} aria-pressed={view==='lines'} onClick={()=>setView('lines')}>叠加走势</button></div><span>按你的习惯切换图表，选择会自动记住</span></div>
      <div className="compare-chart-toolbar"><span>{result?.baseline_date?`${result.baseline_date} — ${result.expected_through} · ${Math.max(0,result.dates.length-1)} 个交易日`:'等待选择与数据对齐'}</span>
        <div className="compare-actions"><button className="button" disabled={busy||result?.refreshing} onClick={()=>setRefresh(v=>v+1)}><RefreshCw size={14} className={busy||result?.refreshing?'spin':''}/>刷新对比</button><button className="button" onClick={exportData} disabled={!result}><Download size={14}/>导出数据</button></div></div>
      {error&&<div className="notice error" role="alert">{error}。保留本次选择的最后有效数据，稍后自动重试。</div>}
      {result?.refreshing&&<p className="compare-message" role="status"><Loader2 size={14} className="spin"/>正在补齐所选对象的日线，完成后自动更新。</p>}
      {result?.warnings.map(w=><p className="compare-message warning-copy" key={w}>{w}</p>)}
      {hasLines?<Chart key={`${view}:${selectionKey}:${windowSize}`} option={view==='candles'?candles:chartOption} height={view==='candles'?candleHeight(result?.series.length||1):450} preserveZoom/>:<div className="empty"><strong>{busy||result?.refreshing?'正在准备对比走势':ids.length?'当前没有可对齐的走势':'添加对象后显示对比图'}</strong><p>只展示已取得的数据；缺失值不填成 0，也不连接缺失区间。</p></div>}
      <p className="compare-footnote">{view==='candles'?'滚轮或底部滑块同时缩放全部 K 线。每个对象单独展示真实开高低收与成交额，比较涨幅大小可切换“叠加走势”。':'鼠标滚轮或底部滑块缩放，点击图例可隐藏曲线。'}页面每分钟检查更新，日线只在收盘后纳入；不是盘中分时图。</p>
    </section>
    {result&&<>
      <section className="panel"><div className="panel-head"><div><h2>区间表现与数据状态</h2><p>区间涨幅使用上方共同起点和截止日；截止日缺数时不以旧值代替。</p></div></div>
        <div className="table-wrap"><table><thead><tr><th>对比对象</th><th>区间涨幅</th><th>有效日线</th><th>最新日线</th><th>数据来源与状态</th></tr></thead><tbody>{result.series.map((s,i)=><tr key={s.id}>
          <td><div className="compare-table-name"><i style={{background:COLORS[i]}}/><strong>{s.name}</strong></div><small className="muted">{TYPES[s.kind]} · {s.symbol}</small></td>
          <td className={s.change_pct==null?'muted':s.change_pct>=0?'positive':'negative'}>{change(s.change_pct)}</td><td>{s.points} / {s.expected_points}</td><td>{s.last_date||'—'}</td>
          <td className="compare-source">{s.source&&/^https:\/\//.test(s.source)?<a href={s.source} target="_blank" rel="noreferrer">{s.source.includes('qq.com')?'腾讯财经':s.source.includes('sina.com')?'新浪财经':'东方财富'} ↗</a>:<span>{s.source||'尚未取得'}</span>}
            <small>{s.adjustment==='qfq'?'前复权收盘价':'指数点位'} · 采集 {stamp(s.fetched_at)}</small><small className={s.state==='ready'?'':'warning-copy'}>{s.refreshing?'正在更新 · ':''}{{ready:'已对齐',partial:'部分日线缺失',stale:'尚未更新至截止日',unavailable:'没有共同区间数据'}[s.state]||s.state}{s.error?' · 更新失败，稍后重试':''}</small>{s.error&&<details><summary>查看原因</summary><p>{s.error}</p></details>}</td>
        </tr>)}</tbody></table></div></section>
      <section className="panel"><div className="panel-head"><div><h2>每日涨跌的相关性</h2><p>接近 +1：更常同向；接近 −1：更常反向；接近 0：线性关系较弱。</p></div></div>
        {ids.length<2?<p className="compare-message">再添加一个对象，即可查看相关性。</p>:<div className="table-wrap"><table className="compare-matrix"><thead><tr><th>同区间日收益</th>{result.series.map(s=><th key={s.id}>{s.name}</th>)}</tr></thead><tbody>{result.series.map(left=><tr key={left.id}><th>{left.name}</th>{result.series.map(right=>{
          const same=left.id===right.id;
          const pair=result.pairs.find(p=>(p.left===left.id&&p.right===right.id)||(p.right===left.id&&p.left===right.id));
          const v=pair?.value;
          return <td key={right.id} title={same?'同一对象':pair?.reason||`${pair?.samples||0} 个共同日收益样本`} style={v!=null?{background:`rgba(${v>=0?'54,90,128':'192,139,60'},${0.04+Math.abs(v)*0.17})`}:undefined}><strong>{same?'—':format(v)}</strong>{!same&&<small>{pair?.samples||0} 日{v==null?' · 不足/不可算':''}</small>}</td>;
        })}</tr>)}</tbody></table></div>}
        <div className="compare-footnote"><p>仅统计双方都有连续两个交易日收盘价的日收益，至少需要 20 个共同样本。相关性只描述所选历史区间，不证明因果，也不代表未来仍会同步。</p>
          <details><summary>计算口径与来源</summary><p>{result.method}</p><p>区间变化＝（当日收盘价 ÷ 共同起点收盘价 − 1）× 100%。复权历史可能随除权更新；这里的研究数据不会改写模拟成交。板块可能包含所选个股，相关性可能来自共同成分。缩放、隐藏图例只改变图表视图，表格和相关性仍按所选日数计算。</p><p>数据接口说明：<a href="https://akshare.akfamily.xyz/data/stock/stock.html" target="_blank" rel="noreferrer">AKShare 股票数据</a>。国内交易日按北京时间对齐，海外联动见“跨市场联动”。</p></details>
        </div>
      </section>
    </>}
  </div>;
}
