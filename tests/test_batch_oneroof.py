import json
from models import PropertyForSale, ImportBatch, PortalFinding
from portals.batch_oneroof import save_result
from tests.test_oneroof_backfill import item

def test_fills_blanks_preserves_corelogic_and_records_evidence(db_session):
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='manual.csv',status='staged',is_active=False)
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='24 Lomandra Street',suburb='Westgate',region='Auckland',floor_area_m2=155,land_area_m2=None)
    db_session.add(p);db_session.commit()
    n=save_result(db_session,p.id,b.id,item())
    db_session.commit();db_session.refresh(p)
    assert n>0 and p.floor_area_m2==155 and p.land_area_m2==405
    findings=db_session.query(PortalFinding).filter_by(property_id=p.id).all()
    assert any(f.field=='land_area_m2' and f.status=='approved' for f in findings)
    assert any(f.field=='floor_area_m2' and f.kind=='conflict' for f in findings)
    assert all(json.loads(f.extra_json)['evidence'] for f in findings)

def test_wrong_unit_cannot_fill(db_session):
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='manual.csv')
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='2/24 Lomandra Street',suburb='Westgate',region='Auckland')
    db_session.add(p);db_session.commit()
    assert save_result(db_session,p.id,b.id,item())==0
    assert p.land_area_m2 is None

def test_paused_job_keeps_cursor_then_resumes(db_session, monkeypatch):
    from contextlib import contextmanager
    from models import IngestJob
    from portals import batch_oneroof as B
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='manual.csv')
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='24 Lomandra Street',suburb='Westgate',region='Auckland')
    db_session.add(p);db_session.flush()
    j=IngestJob(batch_type='for_sale',filename=f'{B.STAGE} (batch {b.id})',batch_id=b.id,status='pending',result_json=json.dumps({'ids':[p.id]}))
    db_session.add(j);db_session.commit()
    @contextmanager
    def session():yield db_session
    monkeypatch.setattr(B,'SessionLocal',session)
    monkeypatch.setattr(B,'engine',db_session.get_bind())
    monkeypatch.setattr(B.source,'fill',lambda *a:(0,'paused'))
    B.run_pending()
    db_session.refresh(j)
    assert j.status=='running' and json.loads(j.result_json).get('index',0)==0
    def fill(db,row):return B.source.apply(row,item())
    monkeypatch.setattr(B.source,'fill',fill)
    B.run_pending();db_session.refresh(j);db_session.refresh(p)
    assert j.status=='completed' and j.rows_filled>0 and p.land_area_m2==405

def test_start_is_independent_of_corelogic_and_reuses_pending(db_session, monkeypatch):
    from types import SimpleNamespace
    from models import IngestJob
    from routers import release as R
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='manual.csv')
    db_session.add(b);db_session.flush()
    db_session.add(PropertyForSale(import_batch_id=b.id,address='24 Lomandra Street',suburb='Westgate',region='Auckland'))
    core=IngestJob(batch_type='for_sale',filename=f'enrich (batch {b.id})',batch_id=b.id,status='running',stage='enrich')
    db_session.add(core);db_session.commit()
    monkeypatch.setattr(R,'enrichable_forsale_batch',lambda *a:b)
    args=dict(region='Auckland',cap=200,admin=SimpleNamespace(id=None),db=db_session)
    first=R.start_oneroof_enrich(**args)
    second=R.start_oneroof_enrich(**args)
    assert first.job_id==second.job_id and first.job_id!=core.id
    assert core.status=='running'
    assert R.enrichment_jobs(region='Auckland',admin=None,db=db_session)=={'corelogic':core.id,'oneroof':first.job_id}
