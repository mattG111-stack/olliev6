import json
import pytest
from models import ImportBatch, PropertyForSale, PortalFinding
from dedupe_forsale import dedupe_forsale_batch


def test_exclude_preserves_records_units_bundles_and_audit(db_session):
    db=db_session
    b=ImportBatch(batch_type='for_sale',region='Auckland',filename='new.csv',status='preview',is_active=False)
    db.add(b);db.flush()
    addresses=['2/24 Example Road','2/24 Example Rd','1/24 Example Road',
               '1 Donald Gavin Way, 52 & 61 Gordon Francis Drive','1 Donald Gavin Way']
    rows=[PropertyForSale(import_batch_id=b.id,address=a,suburb='Westgate',fair_value=1000000,is_held=False) for a in addresses]
    db.add_all(rows);db.commit()
    rows[0].is_held=True;rows[0].hold_reason='Missing floor area';db.commit()
    dry=dedupe_forsale_batch(db,batch_id=b.id,exclude_only=True)
    assert dry.duplicates==1 and dry.excluded==0
    assert db.query(PortalFinding).count()==0
    result=dedupe_forsale_batch(db,batch_id=b.id,dry_run=False,exclude_only=True)
    assert (result.duplicates,result.excluded,result.removed)==(1,1,0)
    assert db.query(PropertyForSale).count()==5
    assert rows[0].hold_reason==f'Source review: Duplicate listing of #{rows[1].id}'
    assert not rows[1].is_held and not rows[2].is_held
    audit=json.loads(db.query(PortalFinding).one().extra_json)
    assert audit['previous_hold_reason']=='Missing floor area'
    assert audit['survivor_id']==rows[1].id
    dedupe_forsale_batch(db,batch_id=b.id,dry_run=False,exclude_only=True)
    assert db.query(PortalFinding).count()==1
    assert rows[0].fair_value==1000000
    b.status='published';b.is_active=True;db.commit()
    with pytest.raises(ValueError):
        dedupe_forsale_batch(db,batch_id=b.id,dry_run=False,exclude_only=True)
