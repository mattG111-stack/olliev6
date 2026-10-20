import json
from pathlib import Path
from models import ImportBatch, PropertyForSale, PropertySold
from routers.dashboards import suburb_trend
from routers.properties import suburb_stats, map_points, suburbs


def test_every_picker_name_across_batches_and_filters(db_session):
    """Exercise every current picker spelling against the actual endpoint functions."""
    names=json.loads((Path(__file__).parent/'fixtures/trend_suburbs.json').read_text())
    assert len(names)==319
    assert check_names(db_session,names)==len(names)


def check_names(db,names):
    batches=[]
    for kind,status,active in [('sold','published',True),('sold','published',True),
                               ('sold','staged',False),('for_sale','archived',False),
                               ('for_sale','published',True),('for_sale','preview',False)]:
        b=ImportBatch(batch_type=kind,region='Auckland',filename='fixture.csv',status=status,is_active=active)
        db.add(b);batches.append(b)
    db.flush()
    old,new,private,prev,live,draft=batches
    for name in names:
        for year in [2024,2025]:
            for i in range(8):
                db.add(PropertySold(import_batch_id=(old if i<4 else new).id,
                    suburb=(name.upper()+' ' if i<4 else name),region='Auckland',
                    address=f'{i+1} Test Road',sale_price=1000000 if i<4 else 1200000,
                    sold_date=f'{year}-06-{i+1:02}',property_type='House',cv_numeric=1000000,
                    days_on_market=40 if i<4 else 60,latitude=-36.8,longitude=174.7))
        for batch,day in [(private,'2025-06-01'),(new,'2099-01-01')]:
            db.add(PropertySold(import_batch_id=batch.id,suburb=name,region='Auckland',sale_price=9000000,sold_date=day))
        for batch in [prev,live,draft]:
            for kind,asking in [('House',1000000),('Apartment',500000)]:
                db.add(PropertyForSale(import_batch_id=batch.id,region='Auckland',suburb=name.upper()+' ',
                    address=f'{kind} Test Road',property_type=kind,floor_area_m2=100,
                    asking_price=asking,market_value=asking,is_held=False))
    db.commit()
    for name in names:
        stats=suburb_stats(name,'Auckland',2025,2025,None,db)
        trend=suburb_trend(name,'Auckland',None,db)
        yearly=json.loads(trend.sold_yearly_json)['points']
        assert stats.sold_count==8 and stats.median_sold==1100000
        assert stats.median_days==50 and stats.active_listings==2
        assert [(p['count'],p['median']) for p in yearly]==[(8,1100000),(8,1100000)]
        assert [p.batch_id for p in trend.points]==[prev.id,live.id]
        house=suburb_trend(name,'Auckland','House',db)
        assert all(p.listing_count==1 and p.median_asking==1000000 for p in house.points)
        assert suburb_stats(name,'Auckland',2024,2025,'House',db).sold_count==16
    # Map resolves names across both public batches, keeps private/future out.
    pts=map_points(dataset='sold',region='Auckland',suburb=names[0],min_beds=None,
                   sold_months=None,limit=20000,db=db)
    assert pts.count==16
    return len(names)


def test_empty_area_is_a_valid_empty_response(db_session):
    stats=suburb_stats('No such suburb','Auckland',None,None,None,db_session)
    trend=suburb_trend('No such suburb','Auckland',None,db_session)
    assert stats.sold_count==0 and stats.active_listings==0
    assert trend.points==[] and trend.sold_yearly_json is None
