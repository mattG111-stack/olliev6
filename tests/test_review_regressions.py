from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from models import User, ImportBatch, PropertyForSale
from security import create_access_token, current_user, admin_from_header
from routers.properties import property_history

def test_password_change_revokes_only_affected_user(db_session):
    db=db_session
    a=User(email='a@example.test',password_hash='synthetic',status='approved',role='admin')
    b=User(email='b@example.test',password_hash='synthetic',status='approved')
    db.add_all([a,b]);db.commit()
    old,_=create_access_token(a.id);other,_=create_access_token(b.id)
    assert current_user(old,db).id==a.id
    a.password_changed_at=datetime.now(timezone.utc);db.commit()
    with pytest.raises(HTTPException): current_user(old,db)
    assert admin_from_header('Bearer '+old,db) is None
    assert current_user(other,db).id==b.id
    fresh,_=create_access_token(a.id)
    assert current_user(fresh,db).id==a.id

def test_history_excludes_unpublished_rows_and_subjects(db_session):
    db=db_session;rows={}
    for state in ('published','archived','staged','preview','portal_review'):
        b=ImportBatch(batch_type='for_sale',filename='synthetic',status=state,is_active=state=='published')
        db.add(b);db.flush()
        p=PropertyForSale(import_batch_id=b.id,address='1 Synthetic Road',suburb='Test',slug_id='synthetic')
        db.add(p);db.flush();rows[state]=p
    db.commit()
    result=property_history(rows['published'].id,db)
    assert {r.batch_id for r in result.points}=={rows[s].import_batch_id for s in ('published','archived')}
    for state in ('staged','preview','portal_review'):
        with pytest.raises(HTTPException): property_history(rows[state].id,db)

    rows['archived'].is_held = True
    db.commit()
    assert len(property_history(rows['published'].id, db).points) == 1
    with pytest.raises(HTTPException): property_history(rows['archived'].id, db)
