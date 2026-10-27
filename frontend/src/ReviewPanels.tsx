import React, {useEffect, useState} from 'react';

type Row = Record<string, any>;
const num=(v:any)=>v==null?'—':Number(v).toFixed(2);
const pct=(v:any)=>v==null?'—':`${v>=0?'+':''}${num(v)}%`;
const time=(v:any)=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
async function get(path:string){const r=await fetch('/api/'+path);if(!r.ok)throw new Error('复盘数据读取失败');return r.json()}
function Box({title,description,children}: {title:string,description?:string,children:React.ReactNode}) {return <section className="panel"><div className="panel-head"><div><h2>{title}</h2>{description&&<p>{description}</p>}</div></div>{children}</section>}
const kindNames:Row={premarket:'盘前名单',intraday_supplement:'盘中观察',closing:'盘后次日研究'};
const stateNames:Row={observing:'观察中',missing:'数据待补齐',unverified:'公司行动待核实',matured:'已成熟'};

export function DailyReviews(){
  const [index,setIndex]=useState<Row>({}),[day,setDay]=useState(''),[review,setReview]=useState<Row>({});
  const [kind,setKind]=useState('premarket'),[all,setAll]=useState(false),[search,setSearch]=useState(''),[error,setError]=useState('');
  useEffect(()=>{let active=true;const load=async()=>{try{const i=await get('reviews');if(active){setIndex(i);setDay(old=>old||i.default_date||i.items?.[0]?.trade_date||'');setError('')}}catch(e:any){if(active)setError(e.message)}};void load();const t=setInterval(load,15000);return()=>{active=false;clearInterval(t)}},[]);
  useEffect(()=>{if(!day)return;let active=true;setReview({});const load=async()=>{try{const r=await get('reviews/'+day);if(active){setReview(r);setError('')}}catch(e:any){if(active)setError(e.message)}};void load();const t=setInterval(load,15000);return()=>{active=false;clearInterval(t)}},[day]);
  const rows=(review.items||[]).filter((r:Row)=>(kind==='all'||r.kind===kind)&&(all||r.in_top_list)&&(!search||(r.candidate.name+r.candidate.code).includes(search)));
  const dates=Array.from(new Set([day,...(index.items||[]).map((r:Row)=>r.trade_date)])).filter(Boolean).sort().reverse() as string[];
  const observed=(code:string)=>(review.execution||[]).flatMap((a:Row)=>{const fills=(a.trades||[]).filter((t:Row)=>t.symbol===code&&t.side==='buy');const obs=(a.observations||[]).filter((o:Row)=>o.code===code);const planned=(a.plans||[]).some((p:Row)=>p.items?.some((c:Row)=>c.code===code));return [{account:a.account,planned,fills,obs}]}).filter((x:Row)=>x.planned||x.fills.length||x.obs.length);
  return <>
    {error&&<div className="notice error">{error}，保留最后成功数据。</div>}
    <Box title="昨日选股 · 每日复盘" description="先检查当时的判断，再观察 1、3、5 个交易日的结果。未到观察期限不算失败。">
      <div className="review-toolbar"><label>决策日期 <select value={day} onChange={e=>setDay(e.target.value)}>{dates.map(d=><option key={d}>{d}</option>)}</select></label><select aria-label="名单类别" value={kind} onChange={e=>setKind(e.target.value)}><option value="all">全部名单类别</option>{Object.entries(kindNames).map(([k,v])=><option key={k} value={k}>{String(v)}</option>)}</select><label><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/> 包含研究池与被过滤股票</label><input placeholder="搜索股票名称或代码" value={search} onChange={e=>setSearch(e.target.value)}/></div>
      <div className="performance-grid">{['1','3','5'].map(h=>{const valid=rows.map((r:Row)=>r.outcome?.horizons?.[h]).filter((v:Row)=>v?.status==='matured');const average=valid.length?valid.reduce((s:number,r:Row)=>s+r.return_pct,0)/valid.length:null;return <div key={h}><span>{h} 日研究平均涨跌</span><strong>{pct(average)}</strong><small>{valid.length} / {rows.length} 个观察已成熟</small></div>})}</div>
      <p className="panel-foot">行情截至 {review.data_through||'等待数据'} · 复盘更新 {time(review.generated_at)}。均值按当前筛选的名单类别统计，同股不同类别分别观察；不是账户收益。</p>
    </Box>
    <Box title="候选表现与执行原因" description={review.method||index.method}>
      {rows.length?<div className="table-wrap"><table><thead><tr><th>股票 / 当时排名</th><th>决策时间 / 名单</th><th>当时建议区间</th><th>研究起算</th><th>1 日</th><th>3 日</th><th>5 日</th><th>执行与判断</th></tr></thead><tbody>{rows.slice(0,120).map((r:Row)=>{const c=r.candidate,o=r.outcome||{},ex=observed(c.code);return <tr key={r.source_id+c.code}><td><strong>{c.name||c.code}</strong><small className="block">{c.code} · {r.rank?`第 ${r.rank} 名`:'已过滤'}</small></td><td>{time(r.decision_at)}<small className="block">{kindNames[r.kind]||r.kind}</small></td><td>{num(c.price_plan?.buy_low)}–{num(c.price_plan?.buy_high)}<small className="block">止损 {num(c.price_plan?.stop)} / 止盈 {num(c.price_plan?.take_profit)}</small></td><td>{o.reference_date||'待定'}<small className="block">开盘 {num(o.reference_price)}</small></td>{['1','3','5'].map(h=>{const v=o.horizons?.[h];return <td key={h}><span className={v?.return_pct==null?'muted':v.return_pct>=0?'positive':'negative'}>{v?.status==='matured'?pct(v.return_pct):stateNames[v?.status]||'等待数据'}</span>{v?.status==='matured'&&<small className="block">区间最高 {pct(v.favorable_pct)}<br/>区间最低 {pct(v.adverse_pct)}{v.benchmark_return_pct!=null&&<><br/>沪深300 {pct(v.benchmark_return_pct)}</>}</small>}</td>})}<td><details><summary>{ex.some((x:Row)=>x.fills.length)?'已有模拟买入':ex.some((x:Row)=>x.obs.length)?'查看未成交原因':r.filtered?.length?'查看过滤依据':'没有执行观测'}</summary><ul className="notes">{(r.filtered||[]).map((s:string)=><li key={s}>{s}</li>)}{ex.map((a:Row)=><li key={a.account}>{a.account==='experiment'?'实验账户':'旧策略'}：{a.fills.length?`买入 ${a.fills.length} 笔`:(a.obs[a.obs.length-1]?.reason||'进入计划，未留下成交观测')}{a.obs.map((v:Row)=><small className="block" key={v.reason}>{v.reason} · {time(v.first_at)}–{time(v.last_at)}</small>)}</li>)}</ul><small>模型 {r.model_version||'rules'}<br/>{r.source_id}</small>{(r.findings||[]).map((s:string)=><p key={s}>{s}</p>)}{(c.reasons||[]).map((s:string)=><p key={s}>{s}</p>)}{(c.events||c.news_events||[]).slice(0,3).map((e:Row,i:number)=><p key={i}>{/^https?:\/\//.test(e.url||'')?<a href={e.url} target="_blank" rel="noreferrer">{e.title}</a>:e.title}<small className="block">消息线索，不等于涨跌原因已证实</small></p>)}</details></td></tr>})}</tbody></table></div>:<div className="empty"><strong>该日期或筛选条件下暂无复盘记录</strong><p>缺少原始决策时不补造推荐。后台会补算已保存名单的后续表现。</p></div>}
      {rows.length>120&&<p className="panel-foot">共有 {rows.length} 条，展示前 120 条；可搜索具体股票。</p>}
    </Box>
    <div className="notice">{review.historical_limitations||'没有执行观测不能推断当时一定可以成交。'} 原始候选版本保留；本页的后续行情结果会随观察期成熟而更新。</div>
  </>;
}

export function ModelReview(){
  const [data,setData]=useState<Row>({}),[error,setError]=useState('');
  useEffect(()=>{let active=true;const load=async()=>{try{const d=await get('models');if(active){setData(d);setError('')}}catch(e:any){if(active)setError(e.message)}};void load();const t=setInterval(load,15000);return()=>{active=false;clearInterval(t)}},[]);
  const t=data.training||{},c=data.control||{},g=c.gate||{};
  const cloud=data.cloud||{},cloudFailed=['failure','cancelled','timed_out','action_required','skipped'].includes(cloud.conclusion);
  const labels:Row={historical:'历史样本外与双倍成本通过',sessions:'至少 60 个有效配对观察日',trades:'至少 30 笔完整交易',positive:'在线净收益为正',beats_rules:'优于同风险规则对照',drawdown:'最大回撤不超过 12%'};
  return <>{error&&<div className="notice error">{error}</div>}<Box title="模型学习与验证" description={data.method}>
    <div className="performance-grid"><div><span>实验账户当前选股</span><strong>{c.active_model?'LightGBM':'规则策略'}</strong><small>{c.active_model||'模型达标前继续积累样本'}</small></div><div><span>训练状态</span><strong>{({running:'训练中',complete:'训练完成',insufficient:'样本不足',failed:'本次训练失败'} as Row)[t.state]||'等待首次训练'}</strong><small>{time(t.finished_at||t.started_at)}</small></div><div><span>在线验证</span><strong>{g.paired_sessions||0} 日 / {g.closed_trades||0} 笔</strong><small>两个同风险研究账户同步观测</small></div></div>
    <p className="panel-foot">{t.reason||t.error||'每周产生训练候选；训练完成不等于获准模拟交易。'}{t.split&&!t.split.ready&&` 当前 ${t.split.available_sessions} / 所需 ${t.split.required_sessions} 个成熟标签交易日。`}</p>
    {(cloudFailed||cloud.error)&&<div className="notice error">{cloud.error?'暂时无法读取云端训练状态。':'最近云端训练任务未成功完成。'} {cloud.notice||'保留已有有效版本。'}{cloud.url&&<a href={cloud.url} target="_blank" rel="noreferrer">查看训练任务</a>}</div>}
    {cloud.status&&<p className="panel-foot">最近云端训练：{cloud.status==='completed'?(cloud.conclusion==='success'?'任务完成':'未完成'):cloud.status==='in_progress'?'运行中':'等待运行'} · {time(cloud.created_at)}。任务完成不代表模型达标。</p>}
    {c.rollback_reason&&<div className="notice error">{c.rollback_reason} · {time(c.rollback_at)}</div>}
    <div className="chip-list">{Object.entries(labels).map(([k,label])=><span className={'tag '+(g.checks?.[k]?'success':'neutral')} key={k}>{g.checks?.[k]?'已通过':'待通过'} · {String(label)}</span>)}</div>
    <p className="panel-foot">收益模型预测约 3 个交易日的超额表现，下行分位数模型辅助控制风险。模型预测不等于中长期目标价或盈利保证。切换只作用于后续新开仓。</p>
  </Box>
  {c.comparison&&<Box title="相同风险预算的在线对照"><div className="table-wrap"><table><thead><tr><th>策略</th><th>收益</th><th>完整交易</th><th>已完成交易净盈亏</th><th>最大回撤</th></tr></thead><tbody>{Object.entries(c.comparison).map(([k,v])=>{const a=v as Row;return <tr key={k}><td>{k==='model'?'模型候选':'规则对照'}</td><td>{pct(a.return_pct)}</td><td>{a.performance?.closed_trades||0}</td><td>{num(a.performance?.net_pnl)}</td><td>{num(a.performance?.max_drawdown_pct)}%</td></tr>})}</tbody></table></div></Box>}
  <Box title="模型版本记录" description="按时间划分训练 / 验证 / 测试，间隔至少 5 个交易日；测试数据不参与训练或早停。">
    {data.models?.length?<div className="table-wrap"><table><thead><tr><th>模型版本</th><th>历史样本外</th><th>同风险规则</th><th>成本翻倍</th><th>历史最大回撤</th><th>检验状态</th></tr></thead><tbody>{data.models.map((m:Row)=><tr key={m.id}><td>{m.id}<small className="block">{m.recipe}</small></td><td>{pct(m.metrics?.model?.return_pct)}</td><td>{pct(m.metrics?.rules?.return_pct)}</td><td>{pct(m.metrics?.double_cost?.return_pct)}</td><td>{num(m.metrics?.model?.max_drawdown_pct)}%</td><td>{m.historical_pass?'历史通过，仍需在线验证':'历史未达标'}</td></tr>)}</tbody></table></div>:<div className="empty"><strong>尚无训练完成的模型</strong><p>保持规则策略运行，不用虚构结果填充模型表现。</p></div>}
    <ul className="notes">{(data.limitations||[]).map((s:string)=><li key={s}>{s}</li>)}</ul>
  </Box></>;
}
