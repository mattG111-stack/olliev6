from io import BytesIO
from sqlalchemy import event
from openpyxl import load_workbook
from models import ImportBatch, PropertyForSale
from audit_export import build


def test_export_keeps_complete_rows_without_fetching_source_blobs(db_session):
    db = db_session
    b = ImportBatch(batch_type='for_sale', region='Auckland', filename='checked.csv', is_active=True, status='published')
    db.add(b); db.flush()
    db.add(PropertyForSale(import_batch_id=b.id,address='1 Test Road',suburb='Riverhead',floor_area_m2=100,property_type='House',asking_price=700000,cv_numeric=900000,fair_value=950000,key_facts='large private source payload',image_urls='large gallery payload'))
    db.commit();db.expunge_all()
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany): queries.append(statement)
    event.listen(db.bind,'before_cursor_execute',capture)
    try:data=build(db)
    finally:event.remove(db.bind,'before_cursor_execute',capture)
    workbook=load_workbook(BytesIO(data),read_only=True)
    assert workbook['Listings'].max_row == 2
    assert any('1 Test Road' in list(r) for r in workbook['Listings'].values)
    selects=[q for q in queries if 'FROM properties_for_sale' in q]
    assert len(selects)==1  # no per-row lazy loads
    assert 'key_facts' not in selects[0] and 'image_urls' not in selects[0]
