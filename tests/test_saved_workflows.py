"""Real HTTP writes against disposable fixtures, never production accounts."""
import pytest
from fastapi.testclient import TestClient
from main import app
from models import ImportBatch, PropertyForSale, User, PageView, AuthSession
from security import create_access_token


@pytest.fixture
def world(db_session):
    db = db_session
    users={}
    for name,role in [('admin','admin'),('buyer','user'),('other','user')]:
        u=User(email=name+'@example.test',password_hash='synthetic',role=role,status='approved')
        db.add(u);db.flush();users[name]=u
    batches={}
    for name,state,active in [('old','archived',False),('private','preview',False),('live','published',True)]:
        b=ImportBatch(batch_type='for_sale',filename=name,region='Auckland',status=state,is_active=active)
        db.add(b);db.flush();batches[name]=b
    def listing(batch,slug,suburb='Riverhead',price=800000,**extra):
        p=PropertyForSale(import_batch_id=batches[batch].id,slug_id=slug,address=slug+' Road',
            suburb=suburb,district='Rodney',property_type='House',floor_area_m2=100,
            beds=3,asking_price=price,**extra)
        db.add(p);db.flush();return p
    listing('old','one',price=1500000)
    listing('private','one');listing('private','two')
    exact=[listing('live','one'),listing('live','two',suburb=' RIVERHEAD ')]
    listing('live','nearby',suburb='Riverhead North')
    listing('live','held',is_held=True)
    db.commit()
    headers={name:{'Authorization':'Bearer '+create_access_token(u.id,db)[0]} for name,u in users.items()}
    db.commit()
    with TestClient(app) as client:
        yield db, client, users, headers, exact


def test_saved_search_exact_area_approved_history_and_ownership(world):
    db,c,users,h,exact=world
    created=c.post('/api/wishlists',headers=h['buyer'],json={
        'name':' My search ','suburb':'Riverhead','district':'rodney','max_price':1000000})
    assert created.status_code==200,created.text
    data=created.json();wid=data['id']
    assert data['name']=='My search'
    assert data['match_count']==2 and data['new_count']==2
    matches=c.get(f'/api/wishlists/{wid}/matches',headers=h['buyer'])
    assert {r['id'] for r in matches.json()}=={r.id for r in exact}
    assert all(r['is_new'] for r in matches.json())
    assert c.get(f'/api/wishlists/{wid}/matches',headers=h['other']).status_code==404
    assert c.delete(f'/api/wishlists/{wid}',headers=h['other']).status_code==404
    assert c.post(f'/api/wishlists/{wid}/seen',headers=h['buyer']).status_code==204
    assert c.get('/api/wishlists/notifications',headers=h['buyer']).json()['total_new']==0
    assert c.delete(f'/api/wishlists/{wid}',headers=h['buyer']).status_code==204
    assert c.get('/api/wishlists',headers=h['buyer']).json()==[]


def test_preferences_round_trip_and_customer_isolation(world):
    _,c,_,h,_=world
    r=c.put('/api/preferences',headers=h['buyer'],json={
        'goals':['underpriced'],'suburbs':['Riverhead'],'max_price':1000000,'min_beds':3})
    assert r.status_code==200,r.text
    saved=c.get('/api/preferences',headers=h['buyer']).json()
    assert saved['goals']==['underpriced'] and saved['max_price']==1000000
    assert c.get('/api/preferences',headers=h['other']).json()['goals']==[]
    assert c.post('/api/preferences/confirm',headers=h['buyer']).status_code==200


def test_admin_account_create_edit_delete_preserves_activity_and_live_book(world):
    db,c,users,h,_=world
    created=c.post('/api/admin/users',headers=h['admin'],json={
        'email':'new@example.com','password':'Synthetic1','full_name':'Synthetic customer'})
    assert created.status_code==201,created.text
    data=created.json();uid=data['id']
    assert 'password_hash' not in data
    assert c.patch(f'/api/admin/users/{uid}',headers=h['buyer'],json={'full_name':'Denied'}).status_code==403
    assert c.patch(f'/api/admin/users/{uid}',headers=h['admin'],json={'full_name':'Updated'}).json()['full_name']=='Updated'
    signed=c.post('/api/auth/sign-in',data={'username':'new@example.com','password':'Synthetic1'})
    assert signed.status_code==200,signed.text
    sh={'Authorization':'Bearer '+signed.json()['access_token']}
    assert c.post('/api/activity/page',headers=sh,json={'path':'/property/123','seconds':12}).status_code==204
    count=db.query(PropertyForSale).count()
    assert c.delete(f'/api/admin/users/{uid}',headers=h['admin']).status_code==204
    db.expire_all()
    assert db.query(PropertyForSale).count()==count
    assert db.query(PageView).filter_by(path='/property').one().user_id is None
    assert db.query(AuthSession).filter_by(user_id=uid).count()==0
    assert c.get('/api/auth/me',headers=sh).status_code==401
    assert c.delete(f"/api/admin/users/{users['admin'].id}",headers=h['admin']).status_code==409
