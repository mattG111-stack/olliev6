from models import ImportBatch, PropertySold, PropertyForSale
from routers.properties import suburb_stats


def test_summary_matches_spellings_across_all_published_sold_batches(db_session):
    db=db_session
    old=ImportBatch(batch_type='sold',region='Auckland',filename='old.csv',status='published',is_active=True)
    latest=ImportBatch(batch_type='sold',region='Auckland',filename='new.csv',status='published',is_active=True)
    private=ImportBatch(batch_type='sold',region='Auckland',filename='private.csv',status='staged',is_active=False)
    fs=ImportBatch(batch_type='for_sale',region='Auckland',filename='live.csv',status='published',is_active=True)
    db.add_all([old,latest,private,fs]);db.flush()
    for batch,name,price,days in [(old,'REMUERA ',2000000,40),(latest,'Remuera',2400000,60),(private,'Remuera',9000000,100)]:
        db.add(PropertySold(import_batch_id=batch.id,region='Auckland',suburb=name,
            sale_price=price,cv_numeric=2000000,sold_date='2025-05-01',days_on_market=days,property_type='House'))
    for asking in [2100000,None]:
        db.add(PropertyForSale(import_batch_id=fs.id,region='Auckland',suburb='Remuera',address='1 Test Road',
            floor_area_m2=180,property_type='House',is_held=False,asking_price=asking))
    db.commit()
    s=suburb_stats('Remuera','Auckland',from_year=2025,to_year=2025,ptype=None,db=db)
    assert s.sold_count==2
    assert s.median_sold==2200000 and s.median_days==50
    assert s.active_listings==2 and s.median_asking==2100000
