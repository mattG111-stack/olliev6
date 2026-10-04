import json
from types import SimpleNamespace
import pandas as pd
import pytest
from assistant import tools

@pytest.mark.parametrize('area,expected', [(None,3),(-8.1,3),(2,3),(8,8)])
def test_pool_floor_and_area(area,expected):
    r=json.loads(tools.pool_policy_adjustment(1750000,'Homes',True,area_pool_percent=area))
    assert r['policy_percent']==expected
    assert r['value']==(1810000 if expected == 3 else 1890000)
    assert r['scenario_only'] is True

def test_no_double_count():
    r=json.loads(tools.pool_policy_adjustment(1750000,'Homes',True,'yes',20))
    assert r['value']==1750000 and r['uplift']==0

def test_no_pool():
    assert json.loads(tools.pool_policy_adjustment(1750000,'Homes',False))['uplift']==0

def test_invalid_base():
    assert 'required' in tools.pool_policy_adjustment(float('nan'),'Homes',True)

def test_cv_uses_ratio_not_loose_median():
    frame=pd.DataFrame([{'suburb':'Riverhead','district':'Rodney'}])
    eng=SimpleNamespace(_n_sub_win={},_n_sub={('Riverhead','House'):3},
        shrunk_cv_ratio=lambda **kw:(1.02,'suburb_shrunk_n3'))
    r=tools._external_cv_estimate({'cv':1675000,'property_type':'House'},'riverhead',frame,eng)
    assert r['value']==1710000
    assert r['unrounded_value']==1708500
    assert r['local_sales']==3
    assert 'not matched' in r['limitations']
    eng._n_sub={}
    assert tools._external_cv_estimate({'cv':1675000},'Riverhead',frame,eng)['available'] is False

def test_loose_three_sale_answer_is_not_property_value(monkeypatch):
    frame=pd.DataFrame([{'suburb':'Riverhead','property_type':'House'}])
    class Session:
        def __enter__(self): return self
        def __exit__(self,*a): pass
    monkeypatch.setattr(tools,'SessionLocal',Session)
    monkeypatch.setattr(tools,'_sold_engine',lambda *a:(frame,SimpleNamespace(
        matched_sold_price=lambda **kw:(1355000,'suburb_beds_baths',3))))
    r=tools.value_property('Riverhead',4,2,260,801)
    assert '1,355,000' not in r
    assert 'CANNOT ANSWER' in r

def test_policy_dispatch_registered():
    r=json.loads(tools.dispatch('pool_policy_adjustment',{'base_value':1750000,'base_source':'Homes','has_pool':True}))
    assert r['value']==1810000

@pytest.mark.parametrize('base,expected', [(1726409,1730000),(1730000,1730000),(1730000.01,1740000)])
def test_rounding_is_ceiling(base,expected):
    assert tools._round_estimate_up(base)==expected

def test_pool_rounds_only_final_total():
    r=json.loads(tools.pool_policy_adjustment(1676125,'Apex CV',True))
    assert r['value']==1730000
    assert r['unrounded_value']==1726408.75

def test_exact_multiple_after_uplift_not_rounded_twice():
    r=json.loads(tools.pool_policy_adjustment(1750000,'Homes',True,area_pool_percent=8))
    assert r['value']==1890000
