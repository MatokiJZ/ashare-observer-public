from copy import deepcopy
from datetime import datetime, timedelta

from ashare import pricing, broker, storage
from ashare.core import TZ, iso, config

AT = datetime(2026,10,13,10,5,tzinfo=TZ)
REGIME = {'state':'normal','exposure':.8}


def candidate(code='600000'):
    return {'code':code,'name':'测试公司','sector':'测试行业','strategy':'pullback','price':20.2,
            'trade_eligible':True,'score':90,'stop_distance':1.2,'daily_rank':1,
            'features':{'close':20.2,'ma10':20.,'ma20':19.6,'atr':.5,'high20':22.,'high60':22.5,'asof':'2026-10-12'}}


def report(org, eps, seen=None):
    return {'id':org+str(eps),'institution':org,'published_at':iso(AT-timedelta(days=10)),
            'first_seen_at':iso(seen or AT-timedelta(days=5)), 'forecast_eps':{'2027':eps}}


def evidence():
    return {'reports':[report('A',1),report('B',1.2)], 'peers':[{'code':str(i),'pe':20+i} for i in range(5)],'peer_fetched_at':iso(AT)}


def planned(code='600000'):
    c=candidate(code)
    c['price_plan']=pricing.make_plan(c,{},AT)
    assert c['price_plan']['status']=='ready'
    return c


def quote(at, price=20, volume=1_000_000, code='600000'):
    return {'code':code,'name':'测试公司','price':price,'bid':price-.01,'ask':price+.01,'limit_up':24,'limit_down':16,
            'market_time':iso(at),'delay_status':'timestamped','cumulative_volume_shares':volume}


def buy(calendar):
    c=planned();q=quote(AT)
    sig=pricing.price_signal(c,q,AT)
    broker.tick([q],[sig],REGIME,calendar,AT)
    assert not broker.account()['positions']
    later=AT+timedelta(minutes=1)
    broker.tick([quote(later,volume=1_100_000)],[],REGIME,calendar,later)
    return broker.account()


def test_forecasts_are_deduplicated_and_not_future_information():
    e=evidence();later=report('A',99,AT+timedelta(seconds=1));e['reports'].append(later)
    v=pricing.value_scenarios(candidate(),e,AT)
    assert v['base']==24.2 and v['institution_count']==2
    e['reports'].append(report('A',2))
    assert pricing.value_scenarios(candidate(),e,AT)['institution_count']==2
    e['peer_fetched_at']=iso(AT+timedelta(seconds=1))
    assert pricing.value_scenarios(candidate(),e,AT)['base'] is None


def test_insufficient_or_nonpositive_forecasts_never_invent_value():
    assert pricing.value_scenarios(candidate(),{},AT)['base'] is None
    e=evidence();e['reports'][0]['forecast_eps']['2027']=-1
    assert pricing.value_scenarios(candidate(),e,AT)['base'] is None
    c=candidate();c['sector']='银行'
    assert pricing.value_scenarios(c,evidence(),AT)['base'] is None


def test_plan_has_ordered_prices_cost_adjusted_rr_and_value_cap():
    p=planned()['price_plan']
    assert p['stop']<p['buy_low']<=p['entry']<=p['buy_high']<p['take_profit']
    assert p['reward_risk']>=1.5
    e=evidence();e['reports']=[report('A',.5),report('B',.5)]
    p=pricing.make_plan(candidate(),e,AT)
    assert p['status']=='observe' and p['take_profit'] is None


def test_daily_top_three_freeze_and_risks_excluded():
    candidates=[planned('60000'+str(i)) for i in range(5)]
    candidates[0]['trade_eligible']=False
    b=pricing.daily_selection(candidates,'r',AT)
    assert [c['code'] for c in b['items']]==['600001','600002','600003']
    assert pricing.daily_selection(list(reversed(candidates)),'new',AT+timedelta(hours=1),b)==b
    candidates[1]['price_plan']['entry']=1
    assert b['items'][0]['price_plan']['entry']!=1


def test_price_only_orders_need_new_observed_volume_and_keep_limit(calendar):
    state=buy(calendar)
    assert state['positions'][0]['price_plan']['id']==planned()['price_plan']['id']
    assert len(broker.view()['trades'])==1
    assert state['positions'][0]['entry_price']<=planned()['price_plan']['buy_high']


def test_slippage_outside_buy_zone_prevents_fill(calendar):
    c=planned();q=quote(AT,price=c['price_plan']['buy_high']-.01)
    broker.tick([q],[pricing.price_signal(c,q,AT)],REGIME,calendar,AT)
    t=AT+timedelta(minutes=1)
    broker.tick([quote(t,price=q['price'],volume=1_100_000)],[],REGIME,calendar,t)
    assert not broker.account()['positions']


def test_volume_decreases_or_cross_day_cannot_supply_capacity():
    order={'created_at':iso(AT)}
    q=quote(AT+timedelta(minutes=1),volume=900000)
    assert broker.quote_capacity(order,q,quote(AT),AT+timedelta(minutes=1),config())==0
    q=quote(AT+timedelta(days=1),volume=2_000_000)
    assert broker.quote_capacity(order,q,quote(AT),AT+timedelta(days=1),config())==0


def test_stop_intent_survives_t1_overnight_and_rebound(calendar):
    buy(calendar)
    stop_time=AT+timedelta(minutes=2)
    broker.tick([quote(stop_time,price=19,volume=1_200_000)],[],REGIME,calendar,stop_time)
    p=broker.account()['positions'][0]
    assert p['exit_intent']['kind']=='stop' and not broker.account()['pending']
    next_day=AT+timedelta(days=1)
    broker.tick([quote(next_day,price=20.1)],[],REGIME,calendar,next_day)
    assert broker.account()['pending'][0]['side']=='sell'
    broker.tick([quote(next_day+timedelta(minutes=1),price=20.1,volume=1_500_000)],[],REGIME,calendar,next_day+timedelta(minutes=1))
    v=broker.view()
    assert not v['positions'] and v['performance']['closed_trades']==1
    trip=v['performance']['closed'][0]
    assert trip['exit_price']!=19 and trip['exit_triggers'][0]['trigger_price']==19
    assert trip['total_fees']>0 and trip['price_plan']


def test_take_profit_and_news_have_auditable_triggers(calendar):
    buy(calendar)
    t=AT+timedelta(minutes=2)
    event={'code':'600000','title':'测试公司重大合同终止公告','published_at':iso(t),'first_seen_at':iso(t),'url':'https://example.org/ann'}
    broker.tick([quote(t,price=21.6,volume=1_300_000)],[],REGIME,calendar,t,events=[event])
    p=broker.account()['positions'][0]
    assert p['exit_intent']['kind']=='event' and p['exit_intent']['event']['url']==event['url']


def test_regular_take_profit_latches_without_news(calendar):
    buy(calendar)
    t=AT+timedelta(minutes=2)
    broker.tick([quote(t,price=22,volume=1_300_000)],[],REGIME,calendar,t)
    assert broker.account()['positions'][0]['exit_intent']['kind']=='take_profit'


def test_daily_bought_symbol_cannot_be_reentered(calendar):
    buy(calendar)
    state=broker.account();state['positions']=[];state['pending']=[]
    storage.set_value('portfolio',state)
    t=AT+timedelta(minutes=2);q=quote(t,volume=1_200_000)
    broker.tick([q],[pricing.price_signal(planned(),q,t)],REGIME,calendar,t)
    broker.tick([quote(t+timedelta(minutes=1),volume=1_500_000)],[],REGIME,calendar,t+timedelta(minutes=1))
    assert len(broker.view()['trades'])==1 and not broker.account()['positions']


def test_offline_gap_preserves_exit_but_never_replays_buy(calendar):
    c=planned();q=quote(AT)
    broker.tick([q],[pricing.price_signal(c,q,AT)],REGIME,calendar,AT)
    t=AT+timedelta(minutes=10)
    broker.tick([quote(t,volume=3_000_000)],[],REGIME,calendar,t)
    assert not broker.account()['positions'] and not broker.account()['pending']


def test_daily_three_new_stocks_limit_is_enforced_in_ledger(calendar):
    cfg=config()
    with storage.database() as con:
        for i in range(cfg['daily_picks']):
            con.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)',(str(i),iso(AT),'60010'+str(i),'buy',100,10,5,'test','pullback'))
    c=planned();q=quote(AT)
    broker.tick([q],[pricing.price_signal(c,q,AT)],REGIME,calendar,AT)
    t=AT+timedelta(minutes=1)
    broker.tick([quote(t,volume=1_500_000)],[],REGIME,calendar,t)
    assert not broker.account()['positions'] and len(broker.view()['trades'])==3


def test_out_of_order_quotes_cannot_rewind_volume_checkpoint(calendar):
    c=planned();q=quote(AT)
    broker.tick([q],[pricing.price_signal(c,q,AT)],REGIME,calendar,AT)
    t=AT+timedelta(seconds=30)
    broker.tick([quote(AT-timedelta(seconds=30),volume=800_000)],[],REGIME,calendar,t)
    assert broker.account()['previous_quotes']['600000']['cumulative_volume_shares']==1_000_000


def test_quote_volume_units_and_missing_values(monkeypatch):
    from ashare import providers
    fields=['0']*50
    fields[1]='测试公司'; fields[2]='600000';fields[6]='12345';fields[30]='20261013100500'
    class Response:
        text='v_sh600000="'+'~'.join(fields)+'";'
    monkeypatch.setattr(providers,'http',lambda *a,**k:Response())
    assert providers.tencent_quotes(['sh600000'])[0]['cumulative_volume_shares']==1_234_500
    fields[6]=''
    Response.text='v_sh600000="'+'~'.join(fields)+'";'
    assert providers.tencent_quotes(['sh600000'])[0]['cumulative_volume_shares'] is None


def test_date_only_report_not_visible_intraday():
    from ashare.research_data import normalize_report
    r=normalize_report({'infoCode':'AP123','publishDate':'2026-10-13 00:00:00','predictNextYearEps':1},2026,iso(AT))
    assert pricing.visible([r],AT)==[]


def test_audit_exports_frozen_plan_quote_and_exit_context(calendar):
    from fastapi.testclient import TestClient
    from ashare.api import app
    buy(calendar)
    body=TestClient(app).get('/api/paper-audit').json()
    assert len(body['trades'])==1
    ctx=next(iter(body['contexts'].values()))
    assert ctx['price_plan']['buy_high'] and ctx['fill_quote']['market_time']
    assert ctx['previous_quote']['cumulative_volume_shares']<ctx['fill_quote']['cumulative_volume_shares']


def test_plan_version_tracks_evidence_changes_but_not_poll_clock():
    c=candidate();e=evidence()
    first=pricing.make_plan(c,e,AT)
    assert pricing.make_plan(c,e,AT+timedelta(minutes=1))['id']==first['id']
    e['reports'][1]['forecast_eps']['2027']=1.3
    changed=pricing.make_plan(c,e,AT)
    assert changed['id']!=first['id']
    assert changed['take_profit']==first['take_profit']  # Even when the short-term exit is unchanged.


def test_research_retry_keeps_last_good_inputs_and_original_timestamp(monkeypatch):
    from ashare import research_data,providers
    import time
    old=evidence();old.update(last_attempt=iso(AT-timedelta(minutes=20)),errors=['暂不可用'],industry_reports=[],orders=[])
    storage.save_snapshot('company_research',{'items':{'600000':old}})
    monkeypatch.setattr(research_data,'now',lambda:AT)
    result={'reports':[],'peers':[],'industry_reports':[],'orders':[], 'errors':['接口更新失败'],
            'diagnostics':{'同行估值':'failed','个股研报':'failed'},'peer_fetched_at':iso(AT+timedelta(seconds=1))}
    monkeypatch.setattr(providers,'call',lambda *a,**k:deepcopy(result))
    row=research_data.enrich([candidate()],time.monotonic()+2)['600000']
    assert row['reports']==old['reports'] and row['peers']==old['peers']
    assert row['peer_fetched_at']==old['peer_fetched_at']
    assert pricing.value_scenarios(candidate(),row,AT)['base']==24.2


def test_stale_peer_sample_is_not_relabelled_current():
    e=evidence();e['peer_fetched_at']=iso(AT-timedelta(days=8))
    v=pricing.value_scenarios(candidate(),e,AT)
    assert v['base'] is None and v['peer_sample']==[]


def test_empty_cloud_research_checkpoint_cannot_erase_local_evidence(tmp_path):
    import os
    from ashare.cloud import pack_checkpoint,unpack_checkpoint
    old=evidence();old['fetched_at']=iso(AT)
    storage.save_snapshot('company_research',{'items':{'600000':old},'fetched_at':iso(AT)})
    local_dir=os.environ['ASHARE_DATA_DIR']
    os.environ['ASHARE_DATA_DIR']=str(tmp_path/'cloud')
    storage.save_snapshot('company_research',{'items':{},'fetched_at':iso(AT+timedelta(minutes=5))})
    bundle=tmp_path/'checkpoint.tar.gz';pack_checkpoint(bundle)
    os.environ['ASHARE_DATA_DIR']=local_dir
    unpack_checkpoint(bundle)
    assert storage.snapshot('company_research')['items']['600000']==old


def test_explicit_analyst_targets_take_precedence_over_broad_sector_pe():
    e=evidence();e['reports'][0].update(target_low=30,target_high=34);e['reports'][1].update(target_low=35,target_high=35)
    e['peers']=[{'code':str(i),'pe':80} for i in range(5)]
    v=pricing.value_scenarios(candidate(),e,AT)
    assert v['method']=='analyst_targets' and v['base']==33.5
    assert v['bear']==32 and v['bull']==35 and '期限未统一' in v['horizon']
    e['reports'][1]['first_seen_at']=iso(AT+timedelta(minutes=1))
    assert pricing.value_scenarios(candidate(),e,AT)['status']=='insufficient'


def test_quote_fallback_uses_recent_roster_without_redownloading_exchange_list(monkeypatch):
    from ashare import providers
    class Ak:
        def stock_zh_a_spot_em(self): raise RuntimeError('source unavailable')
        def stock_info_a_code_name(self): raise AssertionError('Should reuse recent verified roster')
    monkeypatch.setattr(providers,'_ak',lambda:Ak())
    monkeypatch.setattr(providers,'now',lambda:AT)
    monkeypatch.setattr(providers,'tencent_quotes',lambda symbols:[{'code':s[2:],'market_time':iso(AT)} for s in symbols])
    stamp=iso(AT-timedelta(days=1))
    storage.save_snapshot('universe',{'items':[{'code':str(600000+i)} for i in range(2500)],'fetched_at':stamp})
    result=providers.universe()
    assert len(result['items'])==2500 and result['roster_asof']==stamp
