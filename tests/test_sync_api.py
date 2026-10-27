import io
import json
import tarfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ashare.api import app
from ashare.cloud import import_report, pack_checkpoint, unpack_checkpoint
from ashare.core import atomic_json, config, data_dir, digest, iso, now
from ashare.storage import snapshot, set_value, get_value


def report(age):
    time=now().replace(microsecond=0)-timedelta(minutes=age)
    return {"id":time.strftime("%Y-%m-%d-%H%M%S")+"-update","schema_version":1,"strategy_version":config()["strategy_version"],"trade_date":time.date().isoformat(),"generated_at":iso(time),"candidates":[]}


def test_report_import_idempotent_and_never_moves_backward():
    recent=json.dumps(report(1)).encode();older=json.dumps(report(10)).encode()
    assert import_report(recent,digest(recent))
    assert not import_report(recent,digest(recent))
    assert not import_report(older,digest(older))
    assert snapshot("latest_report")["id"]==json.loads(recent)["id"]
    assert len(list((data_dir()/"reports").glob("*.json")))==2


def test_bad_report_hash_does_not_write():
    with pytest.raises(ValueError): import_report(json.dumps(report(1)).encode(),"wrong")
    assert not (data_dir()/"reports").exists()


def test_checkpoint_preserves_private_paper_ledger(tmp_path):
    atomic_json(data_dir()/"calendar.json",{"fetched_at":iso(),"dates":["2026-10-13"]})
    set_value("portfolio",{"cash":123})
    archive=tmp_path/"checkpoint.tar.gz"
    pack_checkpoint(archive)
    unpack_checkpoint(archive)
    assert get_value("portfolio")=={"cash":123}
    with tarfile.open(archive) as tar: assert "state.sqlite" not in tar.getnames()


def test_archive_path_traversal_rejected(tmp_path):
    archive=tmp_path/"evil.tar.gz"
    with tarfile.open(archive,"w:gz") as tar:
        info=tarfile.TarInfo("../escape");info.size=1;tar.addfile(info,io.BytesIO(b"x"))
    with pytest.raises(ValueError): unpack_checkpoint(archive)
    assert not (tmp_path/"escape").exists()


def test_api_works_without_data_and_does_not_invent_candidates():
    with TestClient(app) as client:
        for path in ["overview","health","sectors","candidates","linkages","events","portfolio","reports","alerts"]:
            assert client.get("/api/"+path).status_code==200,path
        assert client.get("/api/candidates").json()["report"]=={}
        assert client.get("/api/portfolio").json()["trades"]==[]
        assert client.get("/api/portfolio").json()["equity"]==100000


def test_cross_site_operations_and_dns_rebinding_rejected():
    with TestClient(app) as client:
        assert client.post("/api/portfolio/enabled",json={"enabled":False},headers={"Origin":"https://evil.invalid"}).status_code==403
        assert client.get("/api/health",headers={"Host":"evil.invalid"}).status_code==403
        assert client.get("/api/stocks/abc").status_code==400
