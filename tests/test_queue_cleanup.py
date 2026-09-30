from datetime import datetime, timezone
from models import PortalListing
from portals.queue_cleanup import selection, delete_selected, undo


def test_cleanup_is_scoped_reversible_and_does_not_delete_rows(db_session):
    db = db_session
    rows = [PortalListing(source='homes', kind=k, status=s, property_id=p, address_key=str(i))
            for i,(k,s,p) in enumerate([('for_sale','pending',None), ('sold','pending',None),
                ('for_sale','approved',123), ('for_sale','priced',456), ('for_sale','pending',789)])]
    db.add_all(rows); db.commit()
    assert selection(db,'for_sale') == [rows[0].id]
    receipt = delete_selected(db,'for_sale',[r.id for r in rows],None)
    assert receipt['ids'] == [rows[0].id]
    assert db.query(PortalListing).count() == 5
    assert rows[1].status == 'pending' and rows[2].status == 'approved' and rows[3].status == 'priced'
    assert undo(db,receipt['ids'],datetime.fromisoformat(receipt['decided_at']),None) == 1
    assert selection(db,'for_sale') == [rows[0].id]
    assert undo(db,receipt['ids'],datetime.fromisoformat(receipt['decided_at']),None) == 0


def test_upload_after_selection_and_later_decision_are_protected(db_session):
    db=db_session
    r=PortalListing(source='homes',kind='sold',status='pending',address_key='one')
    db.add(r);db.commit();ids=selection(db,'sold')
    r.property_id=42;db.commit()
    assert delete_selected(db,'sold',ids,None)['ids'] == []
    r.property_id=None;db.commit()
    receipt=delete_selected(db,'sold',ids,None)
    r.decided_at=datetime(2020,1,1,tzinfo=timezone.utc);db.commit()
    assert undo(db,ids,datetime.fromisoformat(receipt['decided_at']),None) == 0
