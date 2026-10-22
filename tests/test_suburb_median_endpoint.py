from datetime import datetime
from fastapi.testclient import TestClient
from models import ImportBatch, PropertyForSale, PropertySold, User
from main import app
from security import create_access_token


def test_medians_use_visible_listings_and_all_approved_sales(db_session):
    db = db_session
    batches = {}
    for name, kind, state, active in [('live','for_sale','published',True),
        ('old','sold','published',False),('new','sold','published',True),
        ('private','sold','preview',False)]:
        b = ImportBatch(batch_type=kind, region='Auckland', filename=name,
                        status=state, is_active=active)
        db.add(b); db.flush(); batches[name]=b
    for i, suburb in enumerate(['Remuera','REMUERA ',' Remuera','Remuera','Remuera']):
        db.add(PropertyForSale(import_batch_id=batches['live'].id, suburb=suburb,
            property_type='House',floor_area_m2=100,beds=3,baths=2,asking_price=1000000+i*100000))
    for extra in [{'is_held':True},{'link_dead_at':datetime.now()},{'asking_basis':'derived'}]:
        db.add(PropertyForSale(import_batch_id=batches['live'].id, suburb='Remuera',
            property_type='House',floor_area_m2=100,beds=3,baths=2,asking_price=9000000,**extra))
    for batch, address, day, price, beds in [('old','1 Road','2025-01-01',1000000,3),
        ('new','2 Road','2026-01-01',1400000,3),('new','1 ROAD','2025-01-01',1000000,3),
        ('old','3 Road','2025-02-01',2000000,4),('private','4 Road','2025-02-01',9000000,3),
        ('new','5 Road','2099-01-01',9900000,3),('new','6 Road','2025-03-01',3000000,3),
        ('new','6 Road','2025-03-01',4000000,4),('new','7 Road',None,5000000,3)]:
        db.add(PropertySold(import_batch_id=batches[batch].id,address=address,
            suburb=' REMUERA ',sold_date=day,sale_price=price,beds=beds,baths=2))
    user=User(email='median@example.test',password_hash='synthetic',status='approved',role='admin')
    db.add(user); db.commit()
    token,_=create_access_token(user.id,db);db.commit()
    with TestClient(app) as client:
        response=client.get('/api/dashboards/suburb-medians?beds=3&baths=2',
                            headers={'Authorization':'Bearer '+token})
    assert response.status_code == 200, response.text
    assert response.json() == [{'suburb':'Remuera','median_asking':1200000.0,
                                'median_sale':1200000.0,'listing_count':5}]


def test_medians_stay_empty_without_a_published_live_book(db_session):
    from routers.dashboards import suburb_medians
    assert suburb_medians(region='Auckland',limit=30,beds=None,baths=None,db=db_session)==[]
