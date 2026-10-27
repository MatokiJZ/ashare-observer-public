import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import * as echarts from 'echarts/core';
import type { EChartsOption } from 'echarts';
import { LineChart, CandlestickChart, BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, TitleComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
echarts.use([LineChart, CandlestickChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, TitleComponent, CanvasRenderer]);
import { Activity, ArrowDownToLine, ArrowUpRight, Bell, Check, ChevronRight, CircleHelp, Cloud, CloudDownload, Database, Globe2, LayoutDashboard, ListFilter, Loader2, Newspaper, Pause, Play, RefreshCw, Search, ShieldCheck, Wallet, X } from 'lucide-react';
import './style.css';
import {DailyReviews, ModelReview} from './ReviewPanels';
import {ComparisonPanel} from './ComparisonPanel';

type Row = Record<string, any>;
const pages = [
  ['overview','市场总览',LayoutDashboard], ['sectors','行业观察',Activity], ['linkages','跨市场联动',Globe2],
  ['comparison','走势对比',Activity],
  ['candidates','选股研究',ListFilter], ['events','新闻与情绪',Newspaper], ['portfolio','模拟盘复盘',Wallet]
] as const;
const red = '#c45447', green = '#22806a';
const colors = ['#365a80','#c08b3c','#7a729b','#32968c','#c45447','#777d86'];
const fmt = (v: any, digits=2) => v === null || v === undefined || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('zh-CN',{maximumFractionDigits:digits,minimumFractionDigits:digits});
const pct = (v: any) => v == null ? '—' : `${v > 0 ? '+' : ''}${fmt(v)}%`;
const amount = (v: any) => v == null ? '—' : Math.abs(v) >= 1e12 ? `${fmt(v/1e12)} 万亿` : Math.abs(v) >= 1e8 ? `${fmt(v/1e8)} 亿` : `${fmt(v/1e4)} 万`;
const dateTime = (v: any) => v ? new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '尚未更新';
const safeUrl = (s: string) => /^https?:\/\//i.test(s || '') ? s : undefined;
async function api(path: string, init?: RequestInit): Promise<any> {
  const r = await fetch('/api/' + path, init);
  if (!r.ok) throw new Error((await r.json()).detail || '请求失败');
  return r.json();
}

function Chart({option,height=300,preserveZoom=false}: {option: EChartsOption,height?:number,preserveZoom?:boolean}) {
  const el = useRef<HTMLDivElement>(null);
  const ref = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if(!el.current) return;
    ref.current=echarts.init(el.current, undefined, {renderer:'canvas'});
    const obs=new ResizeObserver(()=>ref.current?.resize()); obs.observe(el.current);
    return ()=>{obs.disconnect();ref.current?.dispose();};
  },[]);
  useEffect(()=>{
    const chart=ref.current;
    if(!chart)return;
    const previous=preserveZoom?(chart.getOption()?.dataZoom as any[]|undefined)?.[0]:undefined;
    chart.setOption(option,true);
    if(previous)chart.dispatchAction({type:'dataZoom',start:previous.start,end:previous.end});
  },[option,preserveZoom]);
  return <div ref={el} style={{height,width:'100%'}} role="img" aria-label="数据走势图"/>;
}

function lineOption(series: Row[], percent=false): EChartsOption {
  return {animation:false,color:colors,grid:{left:48,right:24,top:28,bottom:45},tooltip:{trigger:'axis',valueFormatter:(v:any)=>fmt(v)+(percent?'%':'')},
    legend:{bottom:0,itemWidth:16,itemHeight:3,textStyle:{fontSize:11,color:'#7c8490'}},
    xAxis:{type:'time',axisLine:{lineStyle:{color:'#e5e8eb'}},axisLabel:{color:'#8b919a',fontSize:10},splitLine:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{color:'#8b919a',fontSize:10,formatter:(v:number)=>`${fmt(v,percent?1:0)}${percent?'%':''}`},splitLine:{lineStyle:{color:'#edf0f3',type:'dashed'}}},
    series:series.map((s,i)=>({name:s.name,type:'line',showSymbol:false,connectNulls:false,lineStyle:{width:2},areaStyle:series.length===1?{opacity:0.06}:undefined,data:s.points || [],emphasis:{focus:'series'}}))};
}

function Empty({title='暂时没有可用数据',text='数据接入成功后会自动展示；缺失值不会以模拟数据替代。',icon:Icon=Database}: {title?:string,text?:string,icon?:any}) {
  return <div className="empty"><span className="empty-icon"><Icon size={26}/></span><strong>{title}</strong><p>{text}</p></div>;
}
function Tag({children,tone='neutral'}: {children:React.ReactNode,tone?:string}) {return <span className={'tag '+tone}>{children}</span>}
function Panel({title,description,action,children,className=''}: {title:string,description?:string,action?:React.ReactNode,children:React.ReactNode,className?:string}) {return <section className={'panel '+className}><div className="panel-head"><div><h2>{title}</h2>{description&&<p>{description}</p>}</div>{action}</div>{children}</section>}
function Delta({value}: {value:any}) {return <span className={value==null?'muted':value>=0?'positive':'negative'}>{pct(value)}</span>}
function SourceTime({row}: {row:Row}) {return <div className="source-time">{row.source || '来源待核实'} · {row.market_time?'行情时间 ':'采集时间 '}{dateTime(row.market_time || row.fetched_at)}{row.delay_status==='unknown'&&' · 延迟未核实'}{row.delay_status==='unavailable'&&' · 暂不可用'}</div>}

function App() {
  const [page,setPage]=useState('overview');
  const [overview,setOverview]=useState<Row>({});
  const [data,setData]=useState<Row>({});
  const [error,setError]=useState('');
  const [loading,setLoading]=useState(true);
  const [healthOpen,setHealthOpen]=useState(false);
  const [toast,setToast]=useState('');
  const [selected,setSelected]=useState<string|null>(null);
  const [notifications,setNotifications]=useState(false);
  const load=async()=>{
    try {const result=await api('overview');setOverview(result);if(page!=='overview'&&page!=='comparison')setData(await api(page));setError('');}
    catch(e:any){setError(e.message);} finally{setLoading(false);}
  };
  useEffect(()=>{setLoading(true);setData({});void load();const timer=setInterval(()=>void load(),15000);return()=>clearInterval(timer);},[page]);
  useEffect(()=>{if(toast){const t=setTimeout(()=>setToast(''),5000);return()=>clearTimeout(t)}},[toast]);
  const action=async(name:string)=>{try{const result=await api('actions/'+name,{method:'POST'});setToast(result.state==='busy'?'已有任务运行中，请稍候':'任务已开始，结果会自动更新');setTimeout(()=>void load(),1500);}catch(e:any){setToast(e.message)}};
  useEffect(()=>{
    if(!notifications) return;
    let last = new Set<string>(); let first=true;
    const t=setInterval(async()=>{try{const rows=await api('alerts');if(!first)rows.filter((r:Row)=>!last.has(r.id)).forEach((r:Row)=>new Notification('A 股观察',{body:r.message}));last=new Set(rows.map((r:Row)=>r.id));first=false;}catch{}},15000);
    return()=>clearInterval(t);
  },[notifications]);
  const report=overview.report || {}, health=overview.health || {};
  const regime=overview.live_regime || report.regime || {label:'等待数据',state:'halt',reason:'盘前研究尚未完成'};
  const current=report.trade_date===new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
  const navTitle=pages.find(p=>p[0]===page)?.[1];
  return <div className="app">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Activity size={23}/></span><div>A 股观察<small>市场 · 研究 · 复盘</small></div></div>
      <div className="nav-caption">研究工作台</div>
      <nav>{pages.map(([id,label,Icon])=><button key={id} className={page===id?'nav-item active':'nav-item'} aria-label={label} title={label} onClick={()=>setPage(id)}><Icon size={18}/><span>{label}</span>{page===id&&<span className="nav-dot"/>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="mode-card"><Cloud size={18}/><strong>盘前云端准备</strong><p>开机接续监测<br/>研究记录独立保存</p><button onClick={()=>setHealthOpen(true)}>查看运行状态 <ChevronRight size={14}/></button></div><div className="sidebar-foot"><ShieldCheck size={15}/> 仅研究与模拟交易</div></div>
    </aside>
    <main>
      <header className="topbar"><div className="breadcrumb">工作台 <ChevronRight size={13}/> <span>{navTitle}</span></div><div className="top-tools"><button className="status-button" onClick={()=>setHealthOpen(true)}><i className={error?'dot off':'dot'}/>{error?'连接中断':health.busy?'数据更新中':'本地已连接'}</button><button className={'icon-button '+(notifications?'enabled':'')} aria-label="开启浏览器提醒" title="开启浏览器提醒" onClick={async()=>{if(!('Notification' in window)){setToast('当前浏览器不支持系统通知');return;}const p=await Notification.requestPermission();setNotifications(p==='granted');setToast(p==='granted'?'已开启本地风险提醒':'未开启通知，仍可在站内查看');}}><Bell size={18}/></button><span className="avatar">研</span></div></header>
      <div className="content">
        <div className="page-heading"><div><div className="eyebrow">{new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'long'})}</div><h1>{navTitle}</h1><p>{({overview:'从市场全貌，到值得进一步研究的机会。',sectors:'观察资金关注的方向，分清价格变化与经营增长。',linkages:'把不同市场放到同一时间轴，检验联动关系。',comparison:'自由组合股票、行业与指数，观察同向与分化。',candidates:'先判断条件，再考虑交易。每一次选择都有记录。',events:'记录消息出现的时间，观察市场如何回应。',portfolio:'记录可以实际执行的交易，检验判断与成本。'} as Row)[page]}</p></div><div className="page-actions"><button className="button" onClick={()=>void action('sync')}><CloudDownload size={16}/>同步盘前报告</button><button className="button dark" onClick={()=>void action('refresh')} disabled={health.busy}><RefreshCw size={16} className={health.busy?'spin':''}/>更新行情</button></div></div>
        {error&&<div className="notice error">页面连接中断：{error}。保留最后成功的数据；后台会根据自身行情状态决定是否暂停模拟交易。</div>}
        <div className="context-strip"><div><span className="pill-dot"/>沪深主板 <span className="strip-divider"/>1–5 个交易日 <span className="strip-divider"/>10 万元模拟盘</div><button onClick={()=>setHealthOpen(true)}>{health.busy?<Loader2 size={13} className="spin"/>:<Database size={13}/>}数据状态<ChevronRight size={13}/></button></div>
        {loading&&!overview.markets?<Empty title="正在读取研究工作台" text="正在检查本地数据与云端报告。" icon={Loader2}/>:
        <>{page==='overview'&&<Overview overview={overview} regime={regime} current={current} setPage={setPage} select={setSelected} action={action}/>}
        {page==='sectors'&&<Sectors data={data}/>}
        {page==='linkages'&&<Linkages data={data}/>}
        {page==='comparison'&&<ComparisonPanel Chart={Chart}/>}
        {page==='candidates'&&<Candidates dailySelection={data.daily_selection} report={data.report||report} live={data.live} quotes={data.quotes} intraday={data.intraday} select={setSelected} action={action}/>}
        {page==='events'&&<Events data={data}/>}
        {page==='portfolio'&&<Portfolio data={data} reload={load} notify={setToast}/>}</>}
        <footer><span>真实数据 · 明确时间 · 可追溯判断</span><a href="/api/research-snapshot" download><ArrowDownToLine size={13}/>导出对话研究快照</a></footer>
      </div>
    </main>
    {toast&&<div className="toast"><Check size={16}/>{toast}</div>}
    {healthOpen&&<HealthDrawer health={health} report={report} close={()=>setHealthOpen(false)} action={action}/>}
    {selected&&<StockDrawer code={selected} close={()=>setSelected(null)}/>}
  </div>;
}

function CoverageDetail({coverage:c={},history:h={}}:Row) {
  const b=c.breakdown||{};
  return <div className="coverage-detail"><p>{c.covered??0} / {c.total??0} 只 · 至少 120 根有效日线 · 应更新至 {c.expected_date||'待核实'}</p>
    <p>覆盖率反映历史行情是否可用于计算，不是选股成功率。低于 90% 时暂停新增模拟仓位。</p>
    {c.breakdown&&<div className="coverage-reasons">{[['stale','未更新'],['missing','缺失'],['short','不足 120 根'],['invalid','字段异常']].map(([k,label])=><span key={k}>{label} <strong>{b[k]||0}</strong></span>)}</div>}
    {h.state==='running'?<p role="status">正在自动补齐：本轮已更新 {h.refreshed||0} 只，待处理 {Math.max(0,(h.queued||0)-(h.attempted||0))} 只。</p>:h.finished_at&&<p>最近补数 {dateTime(h.finished_at)} · 更新 {h.refreshed||0} 只{h.errors?.length?' · 部分来源失败，将自动重试':''}</p>}
  </div>;
}

function Overview({overview,regime,current,setPage,select,action}:Row) {
  const markets:Row[]=overview.markets?.items||[], stats=overview.stats||{}, report=overview.report||{};
  const coverage=overview.health?.coverage||report.coverage||{};
  const [region,setRegion]=useState('全部');
  const [chartSymbol,setChartSymbol]=useState('sh000300');
  const [chartPeriod,setChartPeriod]=useState('history');
  const selected=markets.find(m=>m.symbol===chartSymbol);
  const history=(selected?.[chartPeriod]||[]).filter((r:Row)=>r.close!=null && (chartPeriod!=='intraday'||String(r.time).slice(0,10)===String(selected?.market_time).slice(0,10))); 
  const visible=markets.filter(m=>region==='全部'||m.market===region);
  return <>
    <div className="metric-grid">
      <div className="metric-card accent"><div className="metric-label">当前研究环境 <ShieldCheck size={17}/></div><div className="metric-value smaller">{regime.label}</div><div className="metric-note">{regime.reason}</div></div>
      <div className="metric-card"><div className="metric-label">全市场成交额 <Activity size={17}/></div><div className="metric-value">{stats.count?amount(stats.amount):'—'}</div><div className="metric-note">行情快照 · {stats.count||0} 只有效报价</div></div>
      <div className="metric-card"><div className="metric-label">上涨 / 下跌家数 <Globe2 size={17}/></div><div className="metric-value"><span className="positive">{stats.count?stats.advancing:'—'}</span><span className="slash">/</span><span className="negative">{stats.count?stats.declining:'—'}</span></div><div className="breadth-track"><span style={{width:stats.count?`${stats.advancing/stats.count*100}%`:'0%'}}/></div></div>
      <div className="metric-card"><div className="metric-label">重点观察名单 <ListFilter size={17}/></div><div className="metric-value">{report.candidates?.length??'—'}<small>只</small></div><div className="metric-note">{report.generated_at?`${current?'当日':'历史'}报告 · ${dateTime(report.generated_at)}`:'等待首份研究报告'}</div></div>
    </div>
    <div className="section-title"><h2>全球市场</h2><div className="segmented">{['全部','A股','美国','日本','韩国','港股'].map(r=><button className={region===r?'selected':''} key={r} onClick={()=>setRegion(r)}>{r}</button>)}</div></div>
    <div className="quote-grid">{visible.length?visible.map(q=><button key={q.symbol} className={'quote-card '+(q.symbol===chartSymbol?'selected':'')} onClick={()=>setChartSymbol(q.symbol)}><div className="quote-name">{q.name}<span>{q.market} · {q.session_state==='open'?'交易中':q.session_state==='closed'?'已收市':'状态待核实'}</span></div><div className="quote-row"><strong>{fmt(q.price)}</strong><Delta value={q.change_pct}/></div><SourceTime row={q}/></button>):<Empty title="正在等待行情接入"/>}</div>
    <div className="two-column"><Panel title={selected?.name?selected.name+' · 走势':'市场走势'} description={chartPeriod==='history'?'日线收盘价；数据时间和可用历史以来源为准':'本地在线期间采样的报价走势；离线区间不填补'} action={<div className="segmented"><button className={chartPeriod==='intraday'?'selected':''} onClick={()=>setChartPeriod('intraday')}>分时采样</button><button className={chartPeriod==='history'?'selected':''} onClick={()=>setChartPeriod('history')}>日线</button></div>}>
      {history.length>1?<Chart option={lineOption([{name:selected?.name,points:history.slice(-120).map((r:Row)=>[r.time||r.date,r.close])}])} height={300}/>:<Empty title="该市场的历史走势尚未取得" text="最新点位可单独展示；不会把单个报价绘制成历史走势。"/>}
    </Panel><Panel title="盘前研究" description={report.generated_at?dateTime(report.generated_at)+' · 独立保存的研究版本':'云端准备，本地接续'} action={<Cloud size={18} className="muted"/>}>
      <div className="report-content"><div className="report-status"><Tag tone={current?'success':'warning'}>{current?'当日版本':report.id?'历史版本':'尚未生成'}</Tag><span>{report.kind==='closing'?'盘后研究':report.kind==='intraday_supplement'?'盘中补充':'盘前观察'}</span></div><h3>{report.id?'先看环境，再确认个股条件':'第一份观察名单正在准备'}</h3><p>{regime.reason}。候选只有在数据完整、盘中条件确认后，才可能进入模拟交易。</p><div className="coverage"><span>主板日线覆盖</span><strong>{coverage.total?fmt(coverage.ratio*100,1)+'%':'—'}</strong></div><div className="progress-track"><span style={{width:`${(coverage.ratio||0)*100}%`}}/></div><CoverageDetail coverage={coverage} history={overview.health?.history}/><div className="report-buttons"><button className="text-button" onClick={()=>setPage('candidates')}>查看选股依据 <ArrowUpRight size={15}/></button><button className="text-button muted" onClick={()=>action('report')}>本地补跑</button></div></div>
    </Panel></div>
    <Panel title="值得进一步观察" description="排名代表候选间的相对排序，不等于上涨概率" action={<button className="text-button" onClick={()=>setPage('candidates')}>全部候选 <ChevronRight size={15}/></button>}>
      <CandidateTable rows={(report.candidates||[]).slice(0,5)} select={select}/>
    </Panel>
    <Panel title="成交与拥挤观察" description={overview.crowding?.note}><div className="detail-list"><div><span>成交额前 20 股占比</span><strong>{overview.crowding?.top20_amount_share==null?'—':pct(overview.crowding.top20_amount_share*100)}</strong></div><div><span>绝对涨跌幅超过 7% 的股票比例</span><strong>{overview.crowding?.extreme_move_share==null?'—':pct(overview.crowding.extreme_move_share*100)}</strong></div><div><span>个股涨跌幅离散度</span><strong>{fmt(overview.crowding?.return_dispersion)} 个百分点</strong></div></div></Panel>
  </>;
}

function CandidateTable({rows,select,intraday,quotes}: {rows:Row[],select:(s:string)=>void,intraday?:Row,quotes?:Row}) {
  if(!rows.length)return <Empty title="当前没有满足条件的候选" text="系统允许少选和空仓。日线或风险资料不足时，会继续积累数据。" icon={ListFilter}/>;
  return <div className="table-wrap"><table><thead><tr><th>股票</th><th>行业 / 策略</th><th>参考收盘</th><th>建议买入 / 有效止盈</th><th>止损 / 净盈亏比</th>{quotes&&<th>最新报价 / 行情时间</th>}<th>相对排序</th><th>当前状态</th><th/></tr></thead><tbody>{rows.map((c,i)=>{const signal=intraday?.fresh?intraday.items?.find((s:Row)=>s.code===c.code):null,q=quotes?.[c.code];return <tr key={c.code} onClick={()=>select(c.code)} tabIndex={0} onKeyDown={e=>e.key==='Enter'&&select(c.code)}><td><div className="stock-name"><span className="rank">{String(i+1).padStart(2,'0')}</span><div><strong>{c.name}</strong><small>{c.code}</small></div></div></td><td>{c.sector}<small className="block">{c.strategy_label}</small></td><td className="numeric">{fmt(c.price)}</td><td><strong>{fmt(c.price_plan?.buy_low)}–{fmt(c.price_plan?.buy_high)}</strong><small className="block">止盈 {fmt(c.price_plan?.take_profit)}</small></td><td>{fmt(c.price_plan?.stop)}<small className="block">盈亏比 {fmt(c.price_plan?.reward_risk)}{c.price_plan?.status==='observe'?' · 仅观察':''}</small></td>{quotes&&<td><strong>{fmt(q?.price)}</strong> <Delta value={q?.change_pct}/><small className="block">{dateTime(q?.market_time)}{q?.market_time&&!q?.fresh?' · 非当前有效报价':''}</small></td>}<td><div className="score"><span style={{width:`${c.score}%`}}/>{fmt(c.score,1)}</div></td><td title={(c.risks||[]).join('；')}><span className={signal?.state==='triggered'?'positive':'muted'}>{signal?.label||c.status}</span>{c.risks?.length>0&&<small className="block candidate-risk">{c.risks[0]}</small>}</td><td><ChevronRight size={15}/></td></tr>})}</tbody></table></div>;
}

function DailyPicks({basket,select,intraday}:Row) {
  const current=basket?.trade_date===new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'});
  return <Panel title="今日自动模拟名单" description={current&&basket.items?.length?`冻结于 ${dateTime(basket.selected_at)} · 最多 3 只，按风险过滤后的排名选取，当天不补位`:'等待当日有效研究和报价，符合条件后冻结最多 3 只'}>
    {current&&basket.items?.length?<div className="daily-picks">{basket.items.map((c:Row)=>{const p=c.price_plan||{},sig=intraday?.fresh?intraday.items?.find((r:Row)=>r.code===c.code):null;return <button className="pick-card" key={c.code} onClick={()=>select(c.code)}><div><span className="eyebrow">当日第 {c.daily_rank} 名 · {c.code}</span><h3>{c.name}</h3></div><div className="pick-price">{fmt(p.buy_low)}–{fmt(p.buy_high)}<small>建议买入区间 / 元</small></div><div className="pick-exits"><span>止损 {fmt(p.stop)}</span><span>止盈 {fmt(p.take_profit)}</span></div><small>{sig?.label||'等待盘中重新确认'} · 净盈亏比 {fmt(p.reward_risk)}</small></button>})}</div>:<Empty title="尚无可执行的当日名单" text="只从资料完整、风险通过且净盈亏比至少 1.5 的候选中选取；允许少选或空仓。下方仍展示观察对象的参考价格。"/>}
  </Panel>;
}

function PricePlan({plan:p,frozen}:Row) {
  if(!p||p.status==='unavailable')return <div className="notice">价格计划尚未形成：{p?.reasons?.join('；')||'等待本版研究更新'}</div>;
  const v=p.valuation||{};
  return <section className="price-plan"><div className="section-title"><h3>{frozen?'今日冻结的交易计划':'参考价格与交易条件'}</h3><Tag tone={p.status==='ready'?'success':'warning'}>{p.status==='ready'?'价格条件合格':'仅观察 / 暂不交易'}</Tag></div>
    <p className="muted">计划日期 {p.trade_date} · 日线截止 {p.data_date} · {p.version}</p>
    <div className="price-grid"><div><span>建议买入价</span><strong>{fmt(p.entry)}</strong><small>允许 {fmt(p.buy_low)}–{fmt(p.buy_high)}</small></div><div><span>有效止损</span><strong>{fmt(p.stop)}</strong><small>常规参考 {fmt(p.regular_stop)}</small></div><div><span>有效止盈</span><strong>{fmt(p.take_profit)}</strong><small>常规参考 {fmt(p.regular_take_profit)}</small></div><div><span>短线条件目标</span><strong>{fmt(p.short_target)}</strong><small>1–5 个交易日</small></div></div>
    <p>扣除假设成本的保守盈亏比 <strong>{fmt(p.reward_risk)}</strong>；实际持仓的保护价会按成交价复核，止损只收紧。</p>
    {!!p.blockers?.length&&<ul className="notes warning-copy">{p.blockers.map((x:string)=><li key={x}>{x}</li>)}</ul>}
    <h3>价格为什么设在这里</h3><ul className="notes">{p.reasons?.map((x:string)=><li key={x}>{x}</li>)}</ul>
    <h3>公司价值与中期价格参考</h3><p className="muted">{v.horizon}。这些情景不代表短线必达价格。</p>
    {v.status==='scenario'?<><div className="price-grid scenarios"><div><span>{v.method==='analyst_targets'?'较低机构参考':'保守假设'}</span><strong>{fmt(v.bear)}</strong></div><div><span>{v.method==='analyst_targets'?'机构中位参考':'基准假设'}</span><strong>{fmt(v.base)}</strong></div><div><span>{v.method==='analyst_targets'?'较高机构参考':'乐观假设'}</span><strong>{fmt(v.bull)}</strong></div></div><p>{v.reason}</p></>:<p className="warning-copy">{v.reason}</p>}
    <ul className="notes">{v.reasons?.map((x:string)=><li key={x}>{x}</li>)}</ul>
    <details className="evidence" open><summary>依据与信息来源</summary><h4>公司研报与盈利预测</h4>{v.reports?.length?v.reports.map((r:Row)=><div className="evidence-row" key={r.id}><a href={safeUrl(r.url)} target="_blank" rel="noreferrer">{r.title} ↗</a><small>{r.institution} · 披露 {dateTime(r.published_at)} · 首次发现 {dateTime(r.first_seen_at)}</small><small>{v.forecast_year} 年预测 EPS：{fmt(r.forecast_eps?.[String(v.forecast_year)],3)} 元{r.target_low||r.target_high?` · 原文目标价 ${fmt(r.target_low)}–${fmt(r.target_high)}（期限未统一）`:''}</small></div>):<p className="muted">未取得符合时点要求的公司预测。</p>}
    {!!v.peer_sample?.length&&<details><summary>查看同行估值样本 · {v.peer_count} 家</summary><p className="muted">{v.peer_basis} · 采集 {dateTime(v.peer_fetched_at)}</p><div className="chip-list">{v.peer_sample.map((r:Row)=><Tag key={r.code}>{r.name||r.code} · {fmt(r.pe)} 倍</Tag>)}</div></details>}
    <h4>行业研究</h4>{v.industry_reports?.length?v.industry_reports.map((r:Row)=><div className="evidence-row" key={r.id}><a href={safeUrl(r.url)} target="_blank" rel="noreferrer">{r.title} ↗</a><small>{r.institution} · {dateTime(r.published_at)} · 作为研究线索，未逐篇核实全文</small></div>):<p className="muted">暂未取得匹配行业的研究报告。</p>}
    <h4>订单与合同披露</h4>{v.orders?.length?v.orders.map((r:Row)=><div className="evidence-row" key={r.id}><a href={safeUrl(r.url)} target="_blank" rel="noreferrer">{r.title} ↗</a><small>{r.stage} · {dateTime(r.published_at)}</small><p>{r.valuation_effect}</p></div>):<p className="muted">本次检索未找到符合条件的订单披露，不代表公司没有订单；未来未披露订单不可知。</p>}
    {!!v.errors?.length&&<p className="warning-copy">数据缺口：{v.errors.join('；')}</p>}</details>
  </section>;
}

function Candidates({report,live,quotes,intraday,select,action,dailySelection}:Row) {
  const [query,setQuery]=useState(''),[strategy,setStrategy]=useState('all'),[choice,setChoice]=useState('live');
  const [versions,setVersions]=useState<Row[]>([]),[liveVersions,setLiveVersions]=useState<Row[]>([]),[archive,setArchive]=useState<Row|null>(null);
  useEffect(()=>{api('reports').then(setVersions).catch(()=>{});api('candidate-versions').then(setLiveVersions).catch(()=>{});},[report.id,live?.id]);
  const shown=archive||(choice==='live'&&live?.id?live:report);
  const isLive=choice==='live'&&!archive;
  const rows=(shown.candidates||[]).filter((c:Row)=>(strategy==='all'||c.strategy===strategy)&&(c.name.includes(query)||c.code.includes(query)||c.sector.includes(query)));
  const choose=async(value:string)=>{setChoice(value);setArchive(null);if(value.startsWith('r:'))setArchive(await api('reports/'+value.slice(2)));if(value.startsWith('l:'))setArchive(await api('candidate-versions/'+value.slice(2)));};
  return <>
    <div className="notice"><ShieldCheck size={17}/><span>盘前报告和盘中候选版本分别留档。当前：{shown.id||'尚未生成'} · 观测于 {dateTime(shown.generated_at)}。{isLive&&!live?.id?'盘中名单尚未生成，暂显示最近报告。':''}</span></div>
    {isLive&&<DailyPicks basket={dailySelection} select={select} intraday={intraday}/>}
    {isLive&&live?.error&&<div className="notice error">{live.error}</div>}
    {isLive&&(live?.coverage?.ratio??report.coverage?.ratio??0)<.9&&<div className="notice error">最新日线覆盖 {fmt((live?.coverage?.ratio??report.coverage?.ratio??0)*100,1)}%，低于 90% 门槛；暂停新增模拟仓位，保留研究与已有持仓的风控。</div>}
    <Panel title={isLive?'动态候选股票':'历史研究记录'} description="网页每 15 秒刷新；在线盘中约每分钟采集重点行情，约 15 分钟重建候选池。实际延迟以行情时间为准。" action={<button className="button" onClick={()=>action('report')}><RefreshCw size={14}/>本地重新研究</button>}>
      <div className="filter-row"><div className="search"><Search size={16}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索股票、代码或行业" aria-label="搜索候选"/></div><select value={strategy} onChange={e=>setStrategy(e.target.value)} aria-label="筛选策略"><option value="all">全部策略</option><option value="pullback">趋势回踩</option><option value="breakout">放量突破</option></select><select aria-label="选择报告版本" value={choice} onChange={e=>void choose(e.target.value)}><option value="live">当前动态名单</option><option value="report">最近云端 / 本地报告</option><optgroup label="报告留档">{versions.map(v=><option key={v.id} value={'r:'+v.id}>{v.id}</option>)}</optgroup><optgroup label="盘中名单留档">{liveVersions.map(v=><option key={v.id} value={'l:'+v.id}>{v.id}</option>)}</optgroup></select></div>
      <CandidateTable rows={rows} select={select} intraday={isLive?intraday:undefined} quotes={isLive?quotes:undefined}/>
      <div className="panel-foot">{isLive&&live?.id?<>研究池 {live.pool_count} 只 · 上次重建 {dateTime(live.pool_generated_at)} · 最近名单变动 {dateTime(live.revision_at)} · 新增 {live.changes?.added?.join('、')||'无'} / 移出 {live.changes?.removed?.join('、')||'无'}</>:'历史表格保留当时判断；打开个股详情查看的是最新行情和最新研究。'}</div>
    </Panel>
    <div className="notice"><Activity size={17}/><span>{live?.method||'盘中会根据最新报价、公司公告及已发现的新闻风险重新评估候选；新闻聚合并非完整覆盖。'} 行业、财务和日线指标按其实际周期更新。每日模拟名单单独冻结；报价在建议区间时产生意图，后续有效报价与实际观测成交增量决定能否模拟成交。</span></div>
    <div className="two-column"><Panel title="趋势回踩" description="先有趋势，再观察回踩"><div className="explanation"><h3>缩量回踩，等待恢复</h3><p>收盘高于 20 日均线，20 日均线高于 60 日均线。缩量回踩后，以 10 日均线附近的建议区间等待到价；结合波动与 20 日趋势设置保护位。</p><Tag>未触发，不买入</Tag></div></Panel><Panel title="放量突破" description="价格和成交需要一起确认"><div className="explanation"><h3>突破区间，检验持续性</h3><p>接近 20 日高点的趋势股票，以高点上方的窄区间等待突破；本版按到价触发，后续成交受实际观测量的 1% 限制。5 分钟量能确认保留为旧版对照研究，不作为本版门槛。</p><Tag>两种策略独立记录</Tag></div></Panel></div>
  </>;
}

function Sectors({data}:Row) {
  const [query,setQuery]=useState(''),[chosen,setChosen]=useState<Row|null>(null),[history,setHistory]=useState<Row[]>([]);
  const rows=(data.items||[]).filter((r:Row)=>r.name.includes(query)).sort((a:Row,b:Row)=>(b.change_pct||0)-(a.change_pct||0));
  const choose=async(r:Row)=>{setChosen(r);setHistory([]);try{const v=await api(`sectors/${r.symbol}/history`);setHistory(v.items||[])}catch{}};
  return <><Panel title="行业强弱" description="东方财富行业分类，包含不同层级；不对重叠行业求和" action={<div className="search compact"><Search size={15}/><input aria-label="筛选行业" placeholder="查找行业" value={query} onChange={e=>setQuery(e.target.value)}/></div>}>
    {rows.length?<div className="sector-grid">{rows.slice(0,60).map((r:Row)=><button key={r.symbol} className="sector-tile" onClick={()=>void choose(r)} style={{background:r.change_pct>=0?`rgba(196,84,71,${Math.min(.3,.04+Math.abs(r.change_pct)/35)})`:`rgba(34,128,106,${Math.min(.3,.04+Math.abs(r.change_pct)/35)})`}}><span>{r.name}</span><strong><Delta value={r.change_pct}/></strong><small>涨 {r.advancing??'—'} / 跌 {r.declining??'—'}</small></button>)}</div>:<Empty/>}
    <div className="panel-foot">展示当前筛选的前 {Math.min(60,rows.length)} / {rows.length} 个行业 · {dateTime(data.fetched_at)} · 实际行情延迟未核实</div>
    </Panel>
    {chosen&&<Panel title={chosen.name+' · 行业走势'} action={<button className="icon-button" onClick={()=>setChosen(null)} aria-label="关闭行业走势"><X size={16}/></button>}>{history.length>1?<Chart option={lineOption([{name:chosen.name,points:history.map(r=>[r.date,r.close])}])}/>:<Empty title="该行业历史走势暂不可用"/>}</Panel>}
    <Panel title="经营增长 · 已披露样本" description={data.growth_note}>{Object.keys(data.growth||{}).length?<div className="table-wrap"><table><thead><tr><th>行业</th><th>股票</th><th>报告期</th><th>营收同比</th><th>利润同比</th><th>披露时间</th></tr></thead><tbody>{Object.entries(data.growth).flatMap(([sector,items])=>(items as Row[]).map(r=><tr key={r.code}><td>{sector}</td><td>{r.code}</td><td>{r.report_date}</td><td><Delta value={r.revenue_growth}/></td><td><Delta value={r.profit_growth}/></td><td>{dateTime(r.published_at)}</td></tr>))}</tbody></table></div>:<Empty title="经营增长数据尚待核实" text="需要有效财报及披露日期。股价涨幅不会被当成行业营收增长。"/>}</Panel>
  </>;
}

function Linkages({data}:Row) {
  const [windowSize,setWindowSize]=useState(60),[symbols,setSymbols]=useState(['sh000300','^GSPC','^N225','^KS11']);
  const markets=[{symbol:'sh000300',name:'沪深 300'},...(data.markets||[]).filter((r:Row)=>r.symbol!=='sh000300')];
  const series=symbols.map(symbol=>{const rows=(data.histories?.[symbol]||[]).slice(-windowSize).filter((r:Row)=>r.close>0);return {name:markets.find((r:Row)=>r.symbol===symbol)?.name||symbol,points:rows.length>1?rows.map((r:Row)=>[r.time||r.date,(r.close/rows[0].close-1)*100]):[]}}).filter(s=>s.points.length>1);
  return <><Panel title="跨市场相对走势" description="各序列以所选可用区间首个数据点为 0%；起点可能不同，请结合时间轴比较" action={<div className="segmented">{[20,60,120].map(n=><button key={n} className={windowSize===n?'selected':''} onClick={()=>setWindowSize(n)}>{n} 日</button>)}</div>}><div className="market-select">{markets.filter((r:Row)=>['sh000300','^GSPC','^IXIC','^N225','^TOPX','^KS11','^KQ11','SOXX','XLF','XLE','XLK'].includes(r.symbol)).map((r:Row)=><label key={r.symbol}><input type="checkbox" checked={symbols.includes(r.symbol)} onChange={()=>setSymbols(symbols.includes(r.symbol)?symbols.filter(s=>s!==r.symbol):[...symbols,r.symbol])}/>{r.name}</label>)}</div>{series.length?<Chart height={370} option={lineOption(series,true)}/>:<Empty title="等待可对齐的历史行情"/>}</Panel>
    <div className="two-column"><Panel title="与沪深 300 的滚动相关性" description={data.method}>{(data.correlations||[]).length?<div className="correlation-list">{(data.correlations||[]).filter((r:Row)=>r.window===windowSize).map((r:Row)=><div key={r.symbol}><span>{markets.find((m:Row)=>m.symbol===r.symbol)?.name||r.symbol}</span><strong>{r.correlation==null?'样本不足':fmt(r.correlation)}</strong><small>{r.samples} 个配对样本</small></div>)}</div>:<Empty title="暂时不能计算相关性" text="需要足够的双方历史样本；缺失值不以零替代。"/>}</Panel><Panel title="产业链映射" description="用于提出研究假设，不预设因果关系"><div className="explanation">{(data.industry_mapping||[]).map((r:Row)=><div key={r.foreign}><h3>{r.foreign}</h3><div className="chip-list">{r.domestic.map((d:string)=><Tag key={d}>{d}</Tag>)}</div><p>{r.basis}</p></div>)}<div className="method-note"><CircleHelp size={17}/><span>海外涨跌本身不会触发买入。统计只使用当时已经可见的信息；能否改善样本外表现仍待验证。</span></div></div></Panel></div>
    <Panel title="行业与外围市场的相关性" description="仅覆盖已经取得日线的行业；按 A 股收盘时已可见的海外收益配对，相关性不等于因果。">{(data.industry_correlations||[]).some((r:Row)=>r.window===windowSize)?<div className="table-wrap"><table><thead><tr><th>A 股行业</th><th>外围市场</th><th>相关系数</th><th>配对样本</th></tr></thead><tbody>{(data.industry_correlations||[]).filter((r:Row)=>r.window===windowSize).map((r:Row)=><tr key={r.sector+r.symbol}><td>{r.sector}</td><td>{r.foreign_name}</td><td>{r.correlation==null?'样本不足':fmt(r.correlation)}</td><td>{r.samples}</td></tr>)}</tbody></table></div>:<Empty title="行业配对样本正在积累"/>}</Panel>
  </>;
}

function Events({data}:Row) {
  const [q,setQ]=useState('');
  const rows=(data.items||[]).filter((r:Row)=>(r.title+' '+r.summary).includes(q));
  return <><div className="notice"><Newspaper size={17}/><span>{data.coverage||'等待新闻接入'}。{data.method||'媒体内容保留出处，尚未核实的消息不自动判定利好或利空。'} 已标记 {data.risk_hits||0} 条待复核风险。</span></div><Panel title="事件时间线" description="同时记录原始发布时间与系统首次发现时间" action={<div className="search compact"><Search size={15}/><input aria-label="搜索新闻" placeholder="搜索新闻或政策" value={q} onChange={e=>setQ(e.target.value)}/></div>}>
    {rows.length?<div className="event-list">{rows.slice(0,80).map((r:Row,i:number)=><article key={(r.url||r.title)+i} className="event"><div className="event-time">{dateTime(r.published_at)}<i/></div><div><div className="event-meta"><Tag>{r.source||'来源待核实'}</Tag><span>首次发现 {dateTime(r.first_seen_at)}</span></div><a href={safeUrl(r.url)} target="_blank" rel="noreferrer"><h3>{r.title}<ArrowUpRight size={15}/></h3></a><p>{r.summary}</p><small>{r.classification}</small><div className="chip-list">{(r.related_sectors||[]).map((v:string)=><Tag key={v}>{v}</Tag>)}{(r.risk_terms||[]).map((v:string)=><Tag key={v} tone="warning">待复核 · {v}</Tag>)}</div></div></article>)}</div>:<Empty title="尚未取得匹配的事件" text="新闻缺失时不会生成情绪结论。"/>}</Panel></>;
}

function Portfolio({data,reload,notify}:Row) {
  const [tab,setTab]=useState('daily'),[account,setAccount]=useState('experiment');
  const selected=(data.accounts||[]).find((a:Row)=>a.account_id===account)||data;
  return <><div className="review-toolbar"><div className="segmented">{[['daily','昨日选股'],['account','模拟账户'],['models','模型改进']].map(([key,label])=><button className={tab===key?'selected':''} key={key} onClick={()=>setTab(key)}>{label}</button>)}</div>{tab==='account'&&<select aria-label="模拟账户" value={account} onChange={e=>setAccount(e.target.value)}><option value="experiment">适度进取实验 · 独立 10 万元</option><option value="baseline">旧策略对照 · 原账户</option></select>}</div>{tab==='daily'?<DailyReviews/>:tab==='models'?<ModelReview/>:<><div className="notice">{account==='experiment'?'单笔计划风险 0.75%；正常 / 谨慎 / 弱市仓位上限 80% / 50% / 20%。8% 回撤降低风险，12% 暂停新增。':'保留原策略与历史账本，用于独立对照。'} 两个账户的资金与交易分别记账。</div><AccountPortfolio data={selected} reload={reload} notify={notify}/></>}</>;
}

function AccountPortfolio({data,reload,notify}:Row) {
  const perf=data.performance||{};
  const toggle=async()=>{try{await api('portfolio/enabled?account='+(data.account_id||'baseline'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!data.enabled})});await reload();}catch(e:any){notify(e.message)}};
  return <><div className="metric-grid"><div className="metric-card"><div className="metric-label">模拟总资产</div><div className="metric-value">{fmt(data.equity)}</div><div className="metric-note">初始资金 100,000 元</div></div><div className="metric-card"><div className="metric-label">累计收益</div><div className="metric-value"><Delta value={data.return_pct}/></div><div className="metric-note">按最近有效报价估值</div></div><div className="metric-card"><div className="metric-label">当前回撤</div><div className="metric-value">{fmt(data.drawdown_pct)}<small>%</small></div><div className="metric-note">{data.account_id==='experiment'?'8% 降风险 · 12% 暂停新增':'5% 检查 · 8% 暂停新增'}</div></div><div className="metric-card"><div className="metric-label">可用现金</div><div className="metric-value">{fmt(data.cash)}</div><div className="metric-note">{data.positions?.length||0} / 5 只持仓</div></div></div>
    <Panel title="账户净值" description={data.mode} action={<button className="button" onClick={()=>void toggle()}>{data.enabled?<Pause size={15}/>:<Play size={15}/>} {data.enabled?'暂停模拟交易':'恢复模拟交易'}</button>}>
      {data.drawdown_halted&&<div className="notice error">回撤达到 {fmt((data.risk_policy?.drawdown_halt||.08)*100,0)}%，新增仓位已锁定，需要复核策略。</div>}
      {(data.curve||[]).length>1?<Chart option={lineOption([{name:'模拟总资产',points:data.curve.map((p:Row)=>[p.ts,p.value])}])}/>:<Empty title="尚未积累净值曲线" text="实际在线监测后记录净值；不会预填历史业绩。" icon={Wallet}/>}
    </Panel>
    <Panel title="当前持仓" description="离线保留持仓；止损无法成交时仍保留风险">{data.positions?.length?<div className="table-wrap"><table><thead><tr><th>股票</th><th>数量</th><th>买入价</th><th>估值价</th><th>止损 / 止盈</th><th>买入日期</th><th>行情时间</th></tr></thead><tbody>{data.positions.map((p:Row)=><tr key={p.code}><td><strong>{p.name}</strong><small className="block">{p.code}</small></td><td>{p.quantity}</td><td>{fmt(p.entry_price)}</td><td>{fmt(p.mark)}</td><td>{fmt(p.stop)} / {fmt(p.take_profit)}{p.exit_intent&&<small className="block warning-copy">{p.exit_intent.reason}<br/>{dateTime(p.exit_intent.triggered_at)} 触发，等待可卖与成交</small>}</td><td>{p.entry_date}</td><td>{dateTime(p.mark_time)}</td></tr>)}</tbody></table></div>:<Empty title="当前空仓" text="数据完整、风险允许且条件触发后才会建立模拟仓位。"/>}</Panel>
    <Panel title="策略检验" description={perf.sample_status||'正在积累真实观测样本'}>
      <div className="performance-grid"><div><span>完整交易 / 观察交易日</span><strong>{perf.closed_trades??0} / {perf.observed_sessions??0}</strong></div><div><span>扣费后胜率</span><strong>{perf.win_rate_pct==null?'—':fmt(perf.win_rate_pct,1)+'%'}</strong></div><div><span>完整交易净盈亏</span><strong>{fmt(perf.net_pnl)} 元</strong></div><div><span>平均盈利 / 平均亏损</span><strong>{fmt(perf.payoff_ratio)}</strong></div><div><span>平均每笔净收益</span><strong>{fmt(perf.expectancy)} 元</strong></div><div><span>历史最大回撤（观测值）</span><strong>{fmt(perf.max_drawdown_pct)}%</strong></div></div>
      <div className="panel-foot">{perf.method}</div>
      {perf.by_strategy?.length>0&&<div className="table-wrap"><table><thead><tr><th>策略 / 版本</th><th>完整交易</th><th>胜率</th><th>净盈亏</th><th>平均超额收益 / 基准样本</th></tr></thead><tbody>{perf.by_strategy.map((r:Row)=><tr key={r.strategy+r.strategy_version+r.selection_version}><td>{r.strategy==='pullback'?'趋势回踩':'放量突破'}<small className="block">{r.strategy_version} · {r.selection_version}</small></td><td>{r.closed_trades}</td><td>{fmt(r.win_rate_pct,1)}%</td><td>{fmt(r.net_pnl)}</td><td><Delta value={r.mean_excess_pct}/> / {r.benchmark_samples}</td></tr>)}</tbody></table></div>}
    </Panel>
    <Panel title="买入到卖出的完整复盘" description="完整交易与每日选股复盘共同用于验证；模型达标后仅接管新开仓" action={<a className="button" href={"/api/paper-audit?account="+(data.account_id||"baseline")} download="模拟交易研究记录.json">导出完整研究记录</a>}>
      {perf.closed?.length?<div className="table-wrap"><table><thead><tr><th>股票 / 策略</th><th>买入 → 卖出</th><th>成交均价</th><th>持有交易日</th><th>扣费净盈亏</th><th>同期沪深 300</th><th>卖出原因</th></tr></thead><tbody>{perf.closed.map((r:Row)=><tr key={r.id}><td>{r.name}<small className="block">{r.code} · {r.strategy_version}</small></td><td>{dateTime(r.entry_time)}<small className="block">→ {dateTime(r.exit_time)}</small></td><td>{fmt(r.entry_price)} → {fmt(r.exit_price)}</td><td>{r.holding_sessions??'—'}</td><td>{fmt(r.net_pnl)} 元<small className="block"><Delta value={r.net_return_pct}/></small>{r.observed_favorable_pct!=null&&<small className="block">已观测最高 {pct(r.observed_favorable_pct)} / 最低 {pct(r.observed_adverse_pct)}<br/>离线波动未计入</small>}</td><td><Delta value={r.benchmark_return_pct}/></td><td>{Array.from(new Set(r.exit_reasons)).join('；')}{(r.findings||[]).map((v:string)=><small className="block" key={v}>{v}</small>)}{r.exit_triggers?.[0]&&<small className="block">首次触发 {dateTime(r.exit_triggers[0].triggered_at)} / {fmt(r.exit_triggers[0].trigger_price)} 元</small>}</td></tr>)}</tbody></table></div>:<Empty title="还没有完成买入和全部卖出的交易" text="尚未卖完的持仓不计胜率。空仓、未触发、数据不足都不会伪造成交。"/>}
    </Panel>
    <Panel title="自动执行状态" description={'最后评估 '+dateTime(data.execution?.fetched_at)}><div className="explanation"><p>{data.enabled?'自动模拟已开启':'模拟交易已暂停'} · 等待成交 {data.pending?.length||0} 笔。每天最多新买 3 只冻结候选，每只当天只买一次。到建议区间后等待后续有效买卖报价和成交量；止损、止盈、目标价、事件风险或最长 5 个交易日触发退出。常规保护为实际买入价下方 5%、上方 8%，技术位可更紧。T+1 和封板仍可能延迟成交。</p><div className="chip-list">{(data.execution?.items||[]).map((r:Row)=><Tag key={r.code} tone={r.state==='blocked'?'warning':'neutral'}>{r.code} · {r.label}</Tag>)}</div></div></Panel>
    <Panel title="成交记录" description="手续费、税费、滑点与容量限制已纳入模拟，费率为配置假设">{data.trades?.length?<div className="table-wrap"><table><thead><tr><th>时间</th><th>代码</th><th>方向</th><th>数量</th><th>成交价</th><th>费用</th><th>依据</th></tr></thead><tbody>{data.trades.map((t:Row)=><tr key={t.id}><td>{dateTime(t.ts)}</td><td>{t.symbol}</td><td><Tag tone={t.side==='buy'?'warning':'success'}>{t.side==='buy'?'买入':'卖出'}</Tag></td><td>{t.quantity}</td><td>{fmt(t.price)}</td><td>{fmt(t.fees)}</td><td>{t.reason}</td></tr>)}</tbody></table></div>:<Empty title="还没有模拟成交" text="离线时段不补造成交，历史回放与此账户分开。"/>}</Panel>
  </>;
}

function HealthDrawer({health,report,close,action}:Row) {
  const [probe,setProbe]=useState<Row>(health.probe||{});
  return <div className="overlay" onClick={close}><aside className="drawer" onClick={e=>e.stopPropagation()}><div className="drawer-head"><div><div className="eyebrow">数据与运行</div><h2>每个结论都有时间</h2></div><button className="icon-button" onClick={close} aria-label="关闭数据状态"><X/></button></div>
    <div className="drawer-body"><h3>云端同步</h3><div className="detail-list"><div><span>私有仓库</span><strong>{health.github_repo||'尚未配置'}</strong></div><div><span>同步状态</span><strong>{({success:'已同步',partial:'部分同步 / 等待补齐',failed:'同步失败',running:'同步中'} as Row)[health.sync?.state]||'等待首次同步'}</strong></div><div><span>最近完成</span><strong>{dateTime(health.sync?.finished_at)}</strong></div><div><span>报告日期</span><strong>{report.trade_date||'尚无报告'}</strong></div><div><span>日历覆盖至</span><strong>{health.calendar_valid_through||'尚未取得'}</strong></div></div>{health.sync?.error&&<p className="error-copy">{health.sync.error}</p>}<button className="button" onClick={()=>action('sync')}><CloudDownload size={15}/>重新同步</button>
    <p className="muted">{health.credential_mode==='dedicated'?'本地已配置专用 GitHub 凭据。':'当前使用本机已有 GitHub 登录。仓库专用只读凭据尚未配置，设置方法见使用说明。'}</p><h3>云端任务</h3><div className="detail-list"><div><span>最近任务</span><strong>{dateTime(health.cloud?.created_at)}</strong></div><div><span>执行结果</span><strong>{health.cloud?.status==='in_progress'?'运行中':({success:'成功',failure:'失败，请查看任务记录',cancelled:'已取消',skipped:'暂停 / 未满足运行条件',timed_out:'执行超时'} as Row)[health.cloud?.conclusion]||'尚未核实'}</strong></div></div>{health.cloud?.url&&<a className="text-button" href={safeUrl(health.cloud.url)} target="_blank" rel="noreferrer">查看私有云端任务记录 <ArrowUpRight size={14}/></a>}<p className="muted">{health.cloud?.notice}</p>
    <h3>数据来源</h3>{Object.entries(health.sources||{}).map(([key,value])=>{const s=value as Row;return <div className="source-card" key={key}><div><strong>{({markets:'全球指数',universe:'全市场股票',sectors:'行业行情',news:'新闻事件'} as Row)[key]||key}</strong><Tag tone={s.error?'warning':s.fetched_at?'success':'neutral'}>{s.error?'最近更新失败':s.fetched_at?'已取得数据':'待获取'}</Tag></div><p>{s.source||'来源见具体记录'}</p><small>成功更新 {dateTime(s.fetched_at)}</small>{s.error&&<p className="error-copy">{s.error}</p>}</div>})}
    <h3>日线覆盖与自动补数</h3><CoverageDetail coverage={health.coverage||report.coverage} history={health.history}/><h3>边界与假设</h3><ul className="notes"><li>免费云端可能延迟或漏跑。</li><li>采集频率不代表上游行情无延迟。</li><li>新闻与财务尚未覆盖所有官方来源。</li><li>模拟交易不连接任何券商账户。</li><li>API 密钥不会发送到网页。</li></ul></div></aside></div>;
}

function StockDrawer({code,close}: {code:string,close:()=>void}) {
  const [stock,setStock]=useState<Row>({}),[period,setPeriod]=useState('daily'),[stockError,setStockError]=useState('');
  useEffect(()=>{let active=true;setStock({});const load=()=>api('stocks/'+code).then(v=>{if(active){setStock(v);setStockError('')}}).catch(()=>{if(active)setStockError('连接中断，当前展示最后成功取得的数据')});void load();const timer=setInterval(()=>void load(),15000);return()=>{active=false;clearInterval(timer)};},[code]);
  const candidate=stock.candidate||{}, rows=stock[period]||[];
  const option:EChartsOption={animation:false,grid:[{left:52,right:20,top:25,height:'62%'},{left:52,right:20,top:'76%',height:'14%'}],tooltip:{trigger:'axis'},axisPointer:{link:[{xAxisIndex:'all'}]},xAxis:[{type:'category',data:rows.map((r:Row)=>r.date),axisLabel:{fontSize:9},axisLine:{lineStyle:{color:'#ddd'}}},{type:'category',gridIndex:1,data:rows.map((r:Row)=>r.date),axisLabel:{show:false}}],yAxis:[{scale:true,splitLine:{lineStyle:{color:'#eee',type:'dashed'}}},{scale:true,gridIndex:1,splitNumber:2,axisLabel:{fontSize:9,formatter:(v:number)=>amount(v)}}],series:[{type:'candlestick',name:'价格',data:rows.map((r:Row)=>[r.open,r.close,r.low,r.high]),itemStyle:{color:red,color0:green,borderColor:red,borderColor0:green}},{type:'bar',name:'成交额',xAxisIndex:1,yAxisIndex:1,data:rows.map((r:Row)=>({value:r.amount,itemStyle:{color:r.close>=r.open?'#dba59e':'#a3cabd'}}))}],dataZoom:[{type:'inside',xAxisIndex:[0,1],start:rows.length>100?60:0,end:100}]};
  return <div className="overlay" onClick={close}><aside className="drawer wide" onClick={e=>e.stopPropagation()}><div className="drawer-head"><div><div className="eyebrow">{code} · {stock.metadata?.sector||candidate.sector||'行业待核实'}</div><h2>{candidate.name||code}</h2></div><button className="icon-button" onClick={close} aria-label="关闭股票详情"><X/></button></div><div className="drawer-body">{stockError&&<div className="notice error">{stockError}</div>}<div className="stock-live"><strong>{fmt(stock.quote?.price)}</strong><Delta value={stock.quote?.change_pct}/><Tag tone={stock.quote?.fresh?'success':'warning'}>{stock.quote?.fresh?'当前有效报价':'最后可见报价 / 待更新'}</Tag></div><SourceTime row={stock.quote||{}}/><p className="chart-note">详情每 15 秒刷新；日 K 保留已完成交易日，5 分钟 K 线按采集结果更新。以下为最新研究条件。</p><div className="section-title"><Tag>{candidate.strategy_label||'个股观察'}</Tag><div className="segmented"><button className={period==='daily'?'selected':''} onClick={()=>setPeriod('daily')}>日 K</button><button className={period==='minutes'?'selected':''} onClick={()=>setPeriod('minutes')}>5 分钟</button></div></div>{rows.length?<Chart option={option} height={340}/>:<Empty title="尚未取得该周期行情"/>}<p className="chart-note">原始价格，不复权。指标遇到疑似除权缺口时暂停选股；鼠标滚轮可缩放。</p><PricePlan plan={stock.daily_candidate?.price_plan||candidate.price_plan} frozen={!!stock.daily_candidate}/><h3>入选依据</h3><ul className="notes">{(candidate.reasons||[]).map((s:string)=><li key={s}>{s}</li>)}</ul><h3>入场条件</h3><p>{candidate.price_plan?'当日冻结名单内、风险检查通过且报价进入建议区间；后续报价含滑点仍在区间内才可买入。':candidate.entry||'尚无可执行入场条件'}</p><h3>判断失效与风险</h3><p>{candidate.invalidation}</p>{candidate.risks?.length?<ul className="notes warning-copy">{candidate.risks.map((r:string)=><li key={r}>{r}</li>)}</ul>:<p className="muted">仍需完成盘中行情确认，并满足组合仓位约束。</p>}<div className="detail-list"><div><span>20 日价格位置</span><strong>{candidate.features?pct(candidate.features.position20*100):'—'}</strong></div><div><span>20 日涨幅</span><strong><Delta value={candidate.features?candidate.features.return20*100:null}/></strong></div><div><span>平均波动幅度 ATR</span><strong>{fmt(candidate.features?.atr)}</strong></div><div><span>20 日成交额中位数</span><strong>{amount(candidate.features?.median_amount20)}</strong></div><div><span>最长持有</span><strong>5 个交易日</strong></div></div></div></aside></div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
