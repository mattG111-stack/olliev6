"""Promote validated sold evidence only; source observations remain immutable."""
import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import text
from models import PropertySold, PortalListing
from trademe import address_key


def identity(row):
    stamp=str(row.get('sold_date') or '').strip().split('T')[0].split(' ')[0]
    try:
        stamp=(date.fromisoformat(stamp) if '-' in stamp[:5] else datetime.strptime(stamp,'%m/%d/%Y').date()).isoformat()
    except ValueError:pass
    return (address_key(row.get('address'),row.get('suburb')),stamp)


def reason(row):
    if row.get('kind')!='sold' or not row.get('address') or not row.get('suburb'):
        return 'Missing sold-property identity'
    try:
        day=date.fromisoformat(str(row.get('sold_date')))
        if day>datetime.now(ZoneInfo('Pacific/Auckland')).date():return 'Future sale date'
        price=row.get('sale_price')
        if isinstance(price,bool) or not math.isfinite(float(price)) or float(price)<=0:
            return 'Invalid disclosed sale price'
    except (ValueError,TypeError):return 'Missing or invalid sale price/date'
    if row.get('conflicts') or row.get('source_conflicts') or row.get('price_flag'):
        return 'Source evidence requires review'
    return None


def accept(db, merged):
    from portals.listings import to_listing, _portal_sold_batch, _CARRIED_SOLD, _price_flag
    # Serialize automatic promoters with writes to the sold table on PostgreSQL.
    if db.bind.dialect.name=='postgresql':
        db.execute(text('LOCK TABLE properties_sold IN SHARE ROW EXCLUSIVE MODE'))
    existing={}
    for sale in db.query(PropertySold.address,PropertySold.suburb,PropertySold.sold_date,PropertySold.sale_price).yield_per(1000):
        existing.setdefault(identity({'address':sale.address,'suburb':sale.suburb,'sold_date':sale.sold_date}),[]).append(sale.sale_price)
    counts={'new':0,'skipped':0,'quarantined':0}
    for item in merged:
        problem=reason(item)
        row=to_listing(item['source'],item,kind='sold') if not problem else None
        if row is not None:problem=_price_flag(db,row)
        key=identity(item)
        if not problem and key in existing:
            if all(p is not None and float(p)==float(item['sale_price']) for p in existing[key]):
                counts['skipped']+=1;continue
            problem='Disclosed price conflicts with an existing sale'
        if problem:
            # Full invalid/conflicting evidence is already retained in observations.
            counts['quarantined']+=1
            if row is not None:
                prior=db.query(PortalListing).filter_by(source=row['source'],kind='sold',address_key=row['address_key'],sold_date=row['sold_date'],status='pending').first()
                if prior is None:db.add(PortalListing(**{**row,'price_flag':problem},status='pending'))
            continue
        batch=_portal_sold_batch(db)
        sale=PropertySold(import_batch_id=batch.id)
        for field in _CARRIED_SOLD:setattr(sale,field,row.get(field))
        sale.beds=int(row['beds']) if row.get('beds') is not None else None
        sale.baths=int(row['baths']) if row.get('baths') is not None else None
        db.add(sale);db.flush()
        staged=db.query(PortalListing).filter_by(source=row['source'],kind='sold',address_key=row['address_key'],sold_date=row['sold_date']).first()
        if staged is not None and staged.status=='rejected':
            db.delete(sale);counts['quarantined']+=1;continue
        if staged is None:staged=PortalListing(**row);db.add(staged)
        elif staged.status!='pending':
            db.delete(sale);counts['quarantined']+=1;continue
        staged.status='approved';staged.property_id=sale.id;staged.decided_at=datetime.now(timezone.utc)
        existing[key]=[item['sale_price']];counts['new']+=1
    return counts
