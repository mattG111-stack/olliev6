import json

from models import PortalListing
from routers.release import new_listings, new_sales


def test_review_exposes_gallery_facts_and_flags_without_mixing_sales(db_session):
    db = db_session
    row = PortalListing(source='oneroof', kind='for_sale', status='pending',
        address='1/2 Example Road', suburb='Example', address_key='unit-example',
        image_url='https://example.com/1.jpg',
        image_urls='https://example.com/1.jpg\nhttps://example.com/2.jpg\njavascript:bad',
        floor_area_m2=210, land_area_m2=882, zoning='Residential', building_age='1990s',
        price_flag='Source area conflict', carspaces=2, description='Source description',
        raw_json=json.dumps({'land_slope_contour': 'Level', 'has_swimming_pool': False,
                            'secret': 'must not be exposed', 'raw_source': {'private': 'hidden'}}))
    sale = PortalListing(source='homes', kind='sold', status='pending',
        address='3 Example Road', suburb='Example', address_key='sold-example',
        sale_price=900000, sold_date='2026-01-02', carspaces=1, estimate=910000,
        image_url='https://example.com/sold.jpg', raw_json='invalid json')
    db.add_all([row, sale]); db.commit()
    output = new_listings(limit=200, admin=None, db=db)
    assert output.pending == 1
    assert len(output.listings) == 1
    actual = output.listings[0]
    assert actual.photos == ['https://example.com/1.jpg', 'https://example.com/2.jpg']
    assert actual.floor_area_m2 == 210 and actual.land_area_m2 == 882
    assert actual.price_flag == 'Source area conflict'
    assert actual.details['zoning'] == 'Residential'
    assert actual.details['land_slope_contour'] == 'Level'
    assert actual.details['has_swimming_pool'] is False
    assert 'secret' not in actual.details and 'raw_source' not in actual.details
    sold = new_sales(limit=200, admin=None, db=db)
    assert sold.pending == 1 and sold.listings[0].price_numeric == 900000
    assert sold.listings[0].listed_date == '2026-01-02'
    assert sold.listings[0].carspaces == 1 and sold.listings[0].estimate == 910000
    assert new_listings(limit=1, offset=1, admin=None, db=db).listings == []
    assert new_listings(limit=1, offset=1, admin=None, db=db).pending == 1
    assert row.status == sale.status == 'pending'
