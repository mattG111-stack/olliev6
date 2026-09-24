import json

from assistant.shortlist import bounded_dispatch
from assistant.tools import search_listings
from models import ImportBatch, PropertyForSale


def seed(db_session):
    active = ImportBatch(batch_type='for_sale', filename='synthetic.csv', is_active=True)
    old = ImportBatch(batch_type='for_sale', filename='old.csv', is_active=False)
    db_session.add_all([active, old]); db_session.flush()
    def add(address, **changes):
        data = dict(import_batch_id=active.id, address=address, suburb='Glen Eden',
                    property_type='House', beds=3, asking_price=700000,
                    fair_value=750000, floor_area_m2=110, land_area_m2=650,
                    sale_method='Fixed price')
        data.update(changes)
        row = PropertyForSale(**data); db_session.add(row)
        return row
    # Ineligible cheap records used to consume the shortlist before filtering.
    add('Auction Road', asking_price=500000, sale_method='Auction')
    add('Negotiation Road', asking_price=510000, sale_method='Price by negotiation')
    add('No Ask Road', asking_price=None, fair_value=520000)
    add('Unknown Method Road', asking_price=530000, sale_method=None)
    selected = [add('1 Valid Road', asking_price=600000),
                add('2 Valid Road', asking_price=610000, sale_method='F'),
                add('3 Valid Road', asking_price=620000, sale_method='Asking price'),
                add('4 Valid Road', asking_price=630000, sale_method='Advertised price')]
    add('Boundary Road', asking_price=800000)
    add('Held Road', asking_price=540000, is_held=True)
    add('Old Road', asking_price=550000, import_batch_id=old.id)
    add('Small Road', asking_price=640000, land_area_m2=200)
    add('Unknown Land Road', asking_price=645000, land_area_m2=None)
    db_session.commit()
    return selected


def test_fixed_asking_filters_apply_before_limit_and_count(db_session):
    selected = seed(db_session)
    out = json.loads(search_listings(suburb='Glen Eden', property_type='house',
        min_beds=3, fixed_price_only=True, max_price=800000,
        max_price_exclusive=True, min_land_m2=600, sort_by='price', limit=3))
    assert [p['id'] for p in out['listings']] == [p.id for p in selected[:3]]
    assert out['total_matches'] == 4
    assert out['returned_count'] == 3
    assert out['price_basis'] == 'recorded asking price'
    assert 'duplicate' in out['total_match_unit']
    inclusive = json.loads(search_listings(fixed_price_only=True, max_price=800000,
        min_land_m2=600, sort_by='price', limit=25))
    assert inclusive['total_matches'] == 5


def test_asking_only_does_not_substitute_estimate_or_imply_fixed_sale(db_session):
    seed(db_session)
    default = json.loads(search_listings(max_price=520000))
    assert any(p['address']=='No Ask Road' for p in default['listings'])
    asking = json.loads(search_listings(max_price=520000, asking_price_only=True))
    assert {p['address'] for p in asking['listings']} == {'Auction Road', 'Negotiation Road'}


def test_wrapper_cannot_hide_later_candidates_or_price_conflicts():
    rows = [{'id':1,'address':'Auction Road','sale_method':'Auction'},
            {'id':2,'address':'Negotiation Road','sale_method':'Negotiation'},
            {'id':3,'address':'Unknown Road','sale_method':None},
            {'id':4,'address':'Valid Road','sale_method':'Fixed price','asking_price':600000},
            {'id':5,'address':'Valid Road','sale_method':'Fixed price','asking_price':650000}]
    raw = json.dumps({'rows':rows,'row_count':5})
    run = bounded_dispatch(lambda *_:raw, 3)
    out = json.loads(run('query_data', {'sql':'select * from properties_for_sale'}))
    assert out['rows'] == rows
    assert out['row_count'] == 5
    assert out['shortlist_limit'] == 3


def test_zero_or_negative_tool_limit_never_removes_query_bound(db_session):
    seed(db_session)
    for limit in [0,-1]:
        out=json.loads(search_listings(limit=limit))
        assert len(out['listings']) == 1
