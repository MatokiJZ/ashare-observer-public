import type {EChartsOption} from 'echarts';

export type CandleSeries = {id:string; name:string; symbol:string; kind:string; adjustment:string;
  candles:([number,number,number,number]|null)[]; amounts:(number|null)[]; candle_points:number; amount_points:number};
const RED='#c74c40', GREEN='#19846a';
const number=(v:number|null|undefined)=>v==null?'缺失':v.toLocaleString('zh-CN',{maximumFractionDigits:2,minimumFractionDigits:2});
const amount=(v:number|null|undefined)=>v==null?'缺失':v===0?'0':v>=1e12?`${(v/1e12).toFixed(2)} 万亿`:v>=1e8?`${(v/1e8).toFixed(2)} 亿`:`${(v/1e4).toFixed(2)} 万`;
export const candleHeight=(count:number)=>count*340+65;

export function candleOption(dates:string[], items:CandleSeries[]):EChartsOption {
  const grid:any[]=[], xAxis:any[]=[], yAxis:any[]=[], series:any[]=[], title:any[]=[];
  items.forEach((s,i)=>{
    const top=i*340;
    const priceAxis=i*2, amountAxis=priceAxis+1;
    title.push({text:`${s.name}  ·  ${s.symbol}`,subtext:`${s.kind==='stock'?'前复权价格 · 元':'指数点位'}  |  K 线 ${s.candle_points}/${dates.length} · 成交额 ${s.amount_points}/${dates.length}`,
      left:22,top:top+9,textStyle:{fontSize:14,color:'#293b50',fontWeight:600},subtextStyle:{fontSize:10,color:'#7c8998'},itemGap:7});
    grid.push({left:72,right:28,top:top+65,height:180},{left:72,right:28,top:top+259,height:42});
    [priceAxis,amountAxis].forEach((index,j)=>{
      xAxis.push({type:'category',gridIndex:index,data:dates,boundaryGap:true,axisLine:{lineStyle:{color:'#ccd6e0'}},
        axisTick:{show:false},axisLabel:{show:j===1,color:'#7c8998',fontSize:10,hideOverlap:true,formatter:(v:string)=>v.slice(5)},
        axisPointer:{label:{show:j===1,formatter:(p:any)=>String(p.value)}}});
    });
    yAxis.push({type:'value',gridIndex:priceAxis,scale:true,splitNumber:3,axisLabel:{color:'#56667a',fontSize:11,formatter:(v:number)=>v.toLocaleString('zh-CN',{maximumFractionDigits:2})},
      splitLine:{lineStyle:{color:'#e3e8ee',type:'dashed'}}},
    {type:'value',gridIndex:amountAxis,min:0,splitNumber:1,axisLabel:{fontSize:9,color:'#87909e',formatter:(v:number)=>amount(v)},
      splitLine:{lineStyle:{color:'#e5eaf0'}},axisPointer:{label:{show:false}}});
    series.push({id:s.id+'-candles',name:s.name,type:'candlestick',xAxisIndex:priceAxis,yAxisIndex:priceAxis,
      data:(s.candles||[]).map(c=>c||['-','-','-','-']),barMaxWidth:14,
      itemStyle:{color:RED,color0:GREEN,borderColor:RED,borderColor0:GREEN,borderWidth:1.4},
      emphasis:{itemStyle:{borderWidth:2}}},
    {id:s.id+'-amounts',name:s.name+'成交额',type:'bar',xAxisIndex:amountAxis,yAxisIndex:amountAxis,barMaxWidth:14,
      data:(s.amounts||[]).map((v,index)=>({value:v,itemStyle:{color:s.candles?.[index]?s.candles[index]![1]>=s.candles[index]![0]?'#d58b82':'#7db4a6':'#a3afbd'}}))});
    if(!s.candle_points) title.push({text:'暂无完整开高低收数据，等待补齐',left:'center',top:top+140,textStyle:{fontSize:12,color:'#97a2af',fontWeight:400}});
    if(!s.amount_points) title.push({text:'成交额缺失',left:'center',top:top+270,textStyle:{fontSize:10,color:'#97a2af',fontWeight:400}});
  });
  const indices=xAxis.map((_,i)=>i);
  return {animation:false,title,grid,xAxis,yAxis,series,
    axisPointer:{link:[{xAxisIndex:'all'}],lineStyle:{color:'#8b9aab',type:'dashed'}},
    tooltip:{trigger:'axis',confine:true,renderMode:'richText',axisPointer:{type:'cross'},
      backgroundColor:'#fff',borderColor:'#d3dce5',textStyle:{color:'#34465b',fontSize:12},
      formatter:(params:any)=>{
        const first=Array.isArray(params)?params[0]:params;
        if(!first)return '';
        const s=items[Math.floor(first.seriesIndex/2)], index=first.dataIndex;
        if(!s)return '';
        const c=s.candles?.[index];
        return `${dates[index]}  ${s.name}\n${c?`开 ${number(c[0])}   收 ${number(c[1])}\n低 ${number(c[2])}   高 ${number(c[3])}`:'开高低收数据缺失'}\n成交额 ${amount(s.amounts?.[index])}`;
      }},
    dataZoom:[{type:'inside',xAxisIndex:indices,filterMode:'none'},
      {type:'slider',xAxisIndex:indices,filterMode:'none',bottom:17,left:72,right:28,height:21,showDetail:false,borderColor:'#d8e0e9'}]};
}
