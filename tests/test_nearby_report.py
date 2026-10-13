import json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from assistant.agent import ask
from assistant.nearby_report import PREFIX,REQUEST,direct_nearby
from periods import sale_day

Q=PREFIX+json.dumps({'kind':'property','id':42})+'. Fetch current evidence before answering. '+REQUEST

def test_direct_nearby_skips_provider_and_future_sales():
    today=datetime.now(ZoneInfo('Pacific/Auckland')).date()
    rows=[dict(address='2/10 Test Road',sale_price=750000,sold_date=str(today-timedelta(days=2))),dict(address='Future',sale_price=900000,sold_date=str(today+timedelta(days=1)))]
    def dispatch(name,args):
        assert args=={'property_id':42}
        return json.dumps({'id':42,'address':'1 Test Road'} if name=='get_property' else {'comparables':rows,'matched_using':{'fallback':'beds_only'}})
    with patch('assistant.agent.dispatch',side_effect=dispatch):
        result=ask(None,Q)
    assert '2/10 Test Road' in result.text and '$750,000' in result.text
    assert 'Future' not in result.text and 'Apex estimate' not in result.text
    assert result.tools_used==['get_property','get_sold_comparables']

def test_scope_and_errors():
    assert direct_nearby(Q,lambda *a: '{}',history=[{'role':'user'}]) is None
    assert direct_nearby('Other question',lambda *a:'{}') is None
    assert 'unavailable' in direct_nearby(Q,lambda *a:'not available').text

def test_date_parser():
    assert str(sale_day('10/03/2026'))=='2026-10-03'
    assert str(sale_day('2026-10-03'))=='2026-10-03'
    assert sale_day('2026-02-31') is None

def test_comparables_filter_before_limiting(db_session):
    from models import ImportBatch,PropertyForSale,PropertySold
    from routers.properties import property_comparables
    live=ImportBatch(batch_type='for_sale',region='Auckland',filename='test',is_active=True,status='published')
    sold=ImportBatch(batch_type='sold',region='Auckland',filename='test',is_active=True,status='published')
    db_session.add_all([live,sold]);db_session.flush()
    p=PropertyForSale(import_batch_id=live.id,region='Auckland',address='Subject',suburb='Test',beds=2,baths=1)
    db_session.add(p);db_session.flush()
    for day in ['2024-05-01','5/2/2024','2026-99-99','2099-01-01']:
        db_session.add(PropertySold(import_batch_id=sold.id,region='Auckland',address=day,suburb='Test',beds=2,baths=1,sold_date=day,sale_price=700000))
    # Future rows in a supported year must be rejected as well.
    db_session.add(PropertySold(import_batch_id=sold.id,region='Auckland',address='Future2026',suburb='Test',beds=2,baths=1,sold_date='2026-12-31',sale_price=900000))
    db_session.commit()
    result=property_comparables(p.id,db=db_session)
    assert [c.address for c in result.comps]==['5/2/2024','2024-05-01']
    assert result.mean_sale_price==700000
