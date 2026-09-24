import json
from datetime import datetime
from assistant.tools import search_listings
from models import ImportBatch, PropertyForSale
from routers.properties import _hide_bad_data


def test_direct_search_matches_visible_page_and_total(db_session):
    db = db_session
    b = ImportBatch(batch_type='for_sale', filename='fixture.csv', is_active=True)
    db.add(b); db.flush()
    for i, extra in enumerate([{}, {}, {'is_held':True}, {'link_dead_at':datetime(2026,1,1)}, {'sold_seen_at':datetime(2026,1,1)}, {'floor_area_m2':None}, {'margin':3}]):
        fields=dict(import_batch_id=b.id,address=f'{i} Example Road',suburb='Glen Eden',floor_area_m2=100,asking_price=600000,fair_value=700000,margin=.16,property_type='House',type_of_title='Freehold')
        fields.update(extra)
        db.add(PropertyForSale(**fields))
    db.commit()
    expected={p.id for p in _hide_bad_data(db.query(PropertyForSale)).filter(PropertyForSale.import_batch_id==b.id).all()}
    result=json.loads(search_listings(suburb='Glen Eden',limit=1))
    assert result['total_matches']==len(expected)==2
    assert result['returned_count']==1
    assert result['listings'][0]['id'] in expected
    assert result['listings'][0]['title']=='Freehold'
    assert result['listings'][0]['condition']=='not verified'
