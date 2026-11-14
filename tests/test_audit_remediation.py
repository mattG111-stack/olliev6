import json
from types import SimpleNamespace as NS
import pytest
from development_evidence import issue
from assistant.fast_valuation import listed_valuation, direct_valuation
from ml.bug_learning import learn, PriorityModel
from ml.evaluate import should_ship
from suburbs import canonical_suburb

@pytest.mark.parametrize('address,land,cv,bad', [('1 Pleasant Road',21109,1400000,True),('2/4 Blossom Lane',1149,1300000,True),('22 Michaels Avenue',1166,2500000,False)])
def test_parcel_evidence(address,land,cv,bad):assert bool(issue(address,land,cv))==bad

def test_valuation_direction_and_actual_panel(monkeypatch):
    import assistant.fast_valuation as f
    monkeypatch.setattr(f,'load_brief',lambda suburb:'RECENT SALES AND MAP '+suburb)
    answer=listed_valuation(dict(id=1,address='22 Michaels Avenue',suburb='Ellerslie',our_value='$2,440,000',cv='$2,500,000',asking='$1,740,000',comps_used=14))
    assert '2.4% below' in answer.text
    assert 'RECENT SALES AND MAP Ellerslie' in answer.text
    assert 'above the recorded CV' not in answer.text

def test_explicit_address_followup_uses_fresh_subject(monkeypatch):
    import assistant.fast_valuation as f
    monkeypatch.setattr(f,'load_brief',lambda s:'MAP '+s)
    def dispatch(tool,args):
        if tool=='find_address':return json.dumps({'on_the_market_now':[{'id':1}]})
        return json.dumps(dict(id=1,address='22 Michaels Avenue',suburb='Ellerslie',our_value='$2,440,000',cv='$2,500,000'))
    r=direct_valuation('What is 22 Michaels Avenue, Ellerslie worth? Include recent nearby sales and average selling time.',dispatch,[NS(role='user',content='Compare other suburbs')])
    assert r and 'MAP Ellerslie' in r.text

@pytest.mark.parametrize('value,expected',[('AUCKLAND CENTAL','Auckland Central'),('St Heliers','Saint Heliers'),('Pt Chevalier','Point Chevalier'),('Auckland','Auckland')])
def test_conservative_suburb_aliases(value,expected):assert canonical_suburb(value)==expected

def test_model_cannot_ship_random_holdout_or_missing_baseline():
    f=dict(n=300,model=5,raw_cv=15,engine=12)
    assert not should_ship({'forward':f,'forward_cut_month':None})[0]
    assert not should_ship({'forward':{**f,'engine':None},'forward_cut_month':150})[0]
    assert not should_ship({'forward':{**f,'model':float('nan')},'forward_cut_month':150})[0]
    assert should_ship({'forward':f,'forward_cut_month':150})[0]

def test_bug_model_really_learns_and_refuses_unverified_reports():
    rows=[NS(id=i,title='crash server database' if i%2 else 'colour spacing label',detail='',page='/',severity='high' if i%2 else 'low',status='fixed',resolution='Tested fix') for i in range(30)]
    model=PriorityModel(rows)
    assert model.predict(NS(title='database crash',detail='',page='/'))=='high'
    assert model.predict(NS(title='colour spacing',detail='',page='/'))=='low'
    result=learn(rows+[NS(id=40,title='database crash',detail='',page='/',severity='normal',status='open',resolution=None)])
    assert result['status']=='validated' and result['suggestions'][0]['suggested_severity']=='high'
    assert learn([])['status']=='insufficient_verified_fixes'

def test_derived_repair_is_reversible_and_idempotent(db_session):
    from models import ImportBatch,PropertyForSale,PortalFinding
    from development_evidence import repair_stored_signals
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='test',status='published',is_active=True)
    db_session.add(b);db_session.flush()
    p=PropertyForSale(import_batch_id=b.id,address='2/4 Blossom Lane',suburb='Manurewa',land_area_m2=1149,cv_numeric=1300000,can_subdivide=True,is_subdividable=True,best_net_gain=1420000)
    db_session.add(p);db_session.commit()
    assert repair_stored_signals(db_session)==1
    assert p.land_area_m2==1149 and p.cv_numeric==1300000
    assert p.best_net_gain is None and not p.can_subdivide
    assert db_session.query(PortalFinding).count()==1
    assert json.loads(db_session.query(PortalFinding).one().extra_json)['previous_derived_fields']['best_net_gain']==1420000
    assert repair_stored_signals(db_session)==0
