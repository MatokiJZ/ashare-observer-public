from datetime import datetime
import json
import time

import pandas as pd
import pytest

from ashare import providers, storage, jobs
from ashare.cloud import import_report, pack_checkpoint, unpack_checkpoint
from ashare.core import TZ, digest, iso, now
from ashare.strategy import screen


def rows():
    return pd.DataFrame([
        {'date':'2026-10-16','open':10.,'close':10.2,'high':10.5,'low':9.8,'volume':1000000.,'amount':10200000.,'change_pct':2.},
        {'date':'2026-10-19','open':11.,'close':11.2,'high':11.5,'low':10.8,'volume':2000000.,'amount':22400000.,'change_pct':9.8}])


def test_normalize_discards_intraday_bar_and_rejects_fake_units():
    at=datetime(2026,10,19,10,tzinfo=TZ)
    result=providers.normalize_history(rows(),'test',at)
    assert [r['date'] for r in result['bars']]==['2026-10-16']
    bad=rows();bad['volume'] /= 100
    with pytest.raises(ValueError,match='单位'):providers.normalize_history(bad,'test',at)
    bad=rows();bad.loc[0,'amount']=None
    with pytest.raises(ValueError):providers.normalize_history(bad,'test',at)


def test_fallback_preserves_real_turnover(monkeypatch):
    monkeypatch.setattr(providers,'history_em',lambda code: (_ for _ in ()).throw(OSError('timeout')))
    monkeypatch.setattr(providers,'history_sina',lambda code: rows())
    monkeypatch.setattr(providers,'now',lambda:datetime(2026,10,19,10,tzinfo=TZ))
    result=providers.history('000001')
    assert 'sina' in result['source']
    assert result['bars'][0]['amount']==10200000
    assert result['fallback_errors'][0]['source']=='eastmoney'


def test_completed_session_holiday_and_daily_publish_boundary(calendar):
    assert calendar.completed(datetime(2026,10,11,17,tzinfo=TZ))=='2026-10-09'
    assert calendar.completed(datetime(2026,10,19,16,9,tzinfo=TZ))=='2026-10-16'
    assert calendar.completed(datetime(2026,10,19,16,10,tzinfo=TZ))=='2026-10-19'


def test_archive_old_strategy_without_promoting_to_current():
    at=now()
    value={'schema_version':1,'strategy_version':'baseline-1.0.0','id':at.strftime('%Y-%m-%d-%H%M%S')+'-update','trade_date':at.date().isoformat(),'generated_at':iso(at),'candidates':[]}
    raw=json.dumps(value).encode()
    assert not import_report(raw,digest(raw))
    assert not storage.snapshot('latest_report')


def test_checkpoint_fills_gap_even_when_local_latest_is_newer(tmp_path):
    storage.save_bars('600000',pd.DataFrame([{'date':'2026-10-12','close':10},{'date':'2026-10-13','close':11}]))
    archive=tmp_path/'checkpoint.tar.gz';pack_checkpoint(archive)
    storage.save_bars('600000',pd.DataFrame([{'date':'2026-10-13','close':12},{'date':'2026-10-14','close':13}]),replace=True)
    unpack_checkpoint(archive,require_bars=True)
    assert storage.bars('600000').close.tolist()==[10,12,13]


def test_empty_checkpoint_rejected_before_writing(tmp_path):
    storage.save_snapshot('universe',{'items':[], 'fetched_at':iso()})
    archive=tmp_path/'checkpoint.tar.gz';pack_checkpoint(archive)
    storage.save_snapshot('universe',{'items':[{'code':'600000'}]})
    with pytest.raises(ValueError,match='没有日线'):unpack_checkpoint(archive,require_bars=True)
    assert storage.snapshot('universe')['items']


def test_coverage_explains_missing_short_invalid_and_stale(calendar):
    at=datetime(2026,10,19,10,tzinfo=TZ)
    days=pd.bdate_range(end='2026-10-16',periods=130)
    f=pd.DataFrame({'date':days.strftime('%Y-%m-%d'),'available_at':days.strftime('%Y-%m-%dT16:10:00+08:00'),'open':10.,'close':10.,'high':10.2,'low':9.8,'volume':1e7,'amount':1e8})
    bad=f.copy();bad.loc[129,'amount']=None
    universe=[{'code':c,'name':c} for c in ['600001','600002','600003','600004','600005']]
    _,coverage=screen(universe,{'600001':f,'600002':f.iloc[:10],'600003':bad,'600004':f.iloc[:-1]}, {},{},calendar,at)
    assert coverage['covered']==1
    assert coverage['breakdown']=={'missing':1,'short':1,'invalid':1,'stale':1}


def test_backfill_failure_does_not_starve_untried_symbols(calendar,monkeypatch):
    storage.save_snapshot('calendar',calendar.payload)
    at=datetime(2026,10,19,10,tzinfo=TZ);monkeypatch.setattr(jobs,'now',lambda:at)
    storage.save_snapshot('history_checks',{'items':{'600001':{'state':'failed','attempted_at':'2026-10-19T09:00:00+08:00'}}})
    calls=[]
    def fetch(op,code,*args,**kwargs):
        calls.append(code)
        return {'source':'test','bars':providers.normalize_history(rows(),'test',at)['bars']}
    monkeypatch.setattr(providers,'call',fetch)
    monkeypatch.setattr(jobs,'config',lambda:{'history_batch_limit':1})
    universe=[{'code':c,'name':c,'price':10,'amount':a} for c,a in [('600001',1e9),('600002',1e8)]]
    status=jobs.warm_histories(universe,time.monotonic()+5)
    assert calls==['600002']
    assert status['pending']==1
    assert storage.snapshot('history_checks')['items']['600002']['state']=='success'


def test_storage_retains_auxiliary_returns_and_rejects_future_bars(monkeypatch):
    at=datetime(2026,10,19,10,tzinfo=TZ)
    monkeypatch.setattr(storage,'now',lambda:at)
    old=rows();old['available_at']=old.date+'T16:10:00+08:00'
    storage.save_bars('600000',old)
    new=old.iloc[:1].drop(columns=['change_pct']).copy();new['close']=10.3
    storage.save_bars('600000',new)
    saved=storage.bars('600000')
    assert saved.date.tolist()==['2026-10-16']
    assert saved.iloc[0].change_pct==2
    assert saved.iloc[0].close==10.3


def test_unknown_secondary_corporate_actions_cannot_enter():
    from ashare.strategy import features
    days=pd.bdate_range(end='2026-10-16',periods=130)
    f=pd.DataFrame({'date':days.strftime('%Y-%m-%d'),'available_at':days.strftime('%Y-%m-%dT16:10:00+08:00'),'open':10.,'close':10.,'high':10.2,'low':9.8,'volume':1e7,'amount':1e8,'source':'https://finance.sina.com.cn/'})
    at=datetime(2026,10,19,10,tzinfo=TZ)
    assert features(f,at)['corporate_action_unverified']
    f['corporate_action_checked']=True;f['corporate_action_event']=False
    assert not features(f,at)['corporate_action_unverified']
    f.loc[129,'corporate_action_event']=True
    assert features(f,at)['corporate_action_unverified']


def test_live_regime_recovers_from_data_halt_but_never_bypasses_risk():
    from ashare.service import current_regime
    at=now()
    storage.save_snapshot('universe',{'fetched_at':iso(at),'items':[{'change_pct':1,'amount':1e8}]})
    report={'regime':{'state':'halt','exposure':0,'reason':'旧版日线不足'}}
    pool={'coverage':{'ratio':.99},'regime':{'state':'normal','exposure':.8,'label':'正常参与'}}
    assert current_regime(report,pool,True,at)['state']=='normal'
    pool['coverage']['ratio']=.2
    assert current_regime(report,pool,True,at)['state']=='halt'
    pool['coverage']['ratio']=.99
    storage.save_snapshot('universe',{'fetched_at':iso(at),'error':'offline','items':[{'change_pct':1,'amount':1e8}]})
    assert current_regime(report,pool,True,at)['state']=='halt'


def test_corporate_actions_parse_provider_trailing_comment(monkeypatch):
    class Response:
        text='var sz000001qfq={"data":[{"d":"2026-07-01","f":"1.0"}]}\n/* provider checksum */'
    monkeypatch.setattr(providers,'http',lambda *a,**kw:Response())
    assert providers.corporate_actions('000001')['dates']==['2026-07-01']


def test_zero_turnover_placeholders_are_not_trading_sessions():
    f=rows();f.loc[0,['volume','amount']]=0
    at=datetime(2026,10,19,17,tzinfo=TZ)
    assert [r['date'] for r in providers.normalize_history(f,'test',at)['bars']]==['2026-10-19']


def test_bulk_history_reads_and_repairs_share_a_file_lock():
    from concurrent.futures import ThreadPoolExecutor
    storage.save_bars('600001',pd.DataFrame([{'date':'2026-10-16','close':10.}]))
    def write():
        for i in range(20):
            storage.save_bars('600001',pd.DataFrame([{'date':'2026-10-16','close':float(i+1),'source':'test','corporate_action_checked':True}]))
    def read():
        for _ in range(20):
            assert len(storage.bars_many(['600001'])['600001'])==1
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(write),pool.submit(read)]
        for task in tasks:task.result()


def test_sina_decoder_supports_runtimes_without_context_manager(monkeypatch):
    import py_mini_racer
    class LegacyDecoder:
        def eval(self, code):pass
        def call(self, *args):return rows().to_dict('records')
    class Response:
        text='var data="encoded";'
    monkeypatch.setattr(py_mini_racer,'MiniRacer',LegacyDecoder)
    monkeypatch.setattr(providers,'http',lambda *args,**kw:Response())
    monkeypatch.setattr(providers,'corporate_actions',lambda code:{'dates':[]})
    f=providers.history_sina('600001')
    assert len(f)==2
    assert f.corporate_action_checked.all()


def test_github_large_report_uses_raw_media_without_base64():
    import httpx
    from ashare.cloud import GitHub
    content=b'{"evidence":"'+b'x'*(1024*1024)+b'"}'
    def handle(request):
        assert request.headers['Accept']=='application/vnd.github.raw+json'
        assert request.url.params['ref']=='data'
        return httpx.Response(200,content=content)
    gh=GitHub.__new__(GitHub);gh.repo='owner/private'
    gh.client=httpx.Client(base_url='https://api.github.com',transport=httpx.MockTransport(handle))
    assert gh.content('reports/report.json')==content
    gh.client.close()
