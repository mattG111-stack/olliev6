"""Promote validated sold evidence only; source observations remain immutable."""
import math
import json
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


def enrich_accepted(db, item):
    """Fill missing facts on a direct-source sale; never rewrite CSV records."""
    from portals.direct import merge_records
    from portals.page_data import match_key, present
    from portals.listings import to_listing, _CARRIED_SOLD
    wanted=match_key(item)
    if wanted is None:return None, False
    candidates=[]
    for staged in db.query(PortalListing).filter_by(kind='sold',status='approved',
            address_key=address_key(item.get('address'),item.get('suburb'))):
        try:prior=json.loads(staged.raw_json or '{}')
        except (ValueError,TypeError):continue
        if not prior.get('_apex_direct') or match_key(prior)!=wanted or identity(prior)!=identity(item):continue
        target=db.get(PropertySold,staged.property_id) if staged.property_id else None
        if target is not None:candidates.append((staged,prior,target))
    if not candidates:return None, False
    if len(candidates)!=1:return 'Multiple existing direct sales require review', False
    staged,prior,target=candidates[0]
    if identity({'address':target.address,'suburb':target.suburb,'sold_date':target.sold_date})!=identity(item) or target.sale_price!=item.get('sale_price'):
        return 'Accepted sale link no longer matches its recorded transaction', False
    snapshots=prior.get('source_snapshots') or [prior]
    incoming=item.get('source_snapshots') or [item]
    # Repeated observations already have immutable rows. Do not indefinitely
    # grow approved evidence just because collection timestamps changed.
    transient={'scraped_at','raw_source','provenance','collection_scope'}
    def facts(snapshot):
        return json.dumps({k:v for k,v in snapshot.items() if k not in transient},sort_keys=True)
    seen={facts(snapshot) for snapshot in snapshots}
    retained=list(snapshots)
    for snapshot in incoming:
        signature=facts(snapshot)
        if signature not in seen:retained.append(snapshot);seen.add(signature)
    combined=merge_records(retained)
    if len(combined)!=1 or reason(combined[0]):
        return 'Later source evidence conflicts with the accepted sale', False
    evidence=combined[0]
    row=to_listing(evidence['source'],evidence,kind='sold')
    if row is None:return 'Later source evidence cannot be normalized', False
    fields=tuple(f for f in _CARRIED_SOLD if f not in ('address','suburb','district','sale_price','sold_date','url'))+('beds','baths')
    # Reject changed stored pricing facts before filling any missing field.
    pricing={'beds','baths','floor_area_m2','land_area_m2','building_age','type_of_title',
             'zoning','property_type','cv_numeric','land_value_numeric','improvement_value_numeric','days_on_market'}
    for field in pricing:
        old,new=getattr(target,field,None),row.get(field)
        if field in {'beds','baths','floor_area_m2','land_area_m2','building_age',
                     'cv_numeric','land_value_numeric','improvement_value_numeric','days_on_market'} and present(old) and present(new):
            try:old,new=float(old),float(new)
            except (TypeError,ValueError):pass
        if present(old) and present(new) and old!=new:
            return 'Later source facts conflict with the stored sale', False
    changed=False
    for field in fields:
        value=row.get(field)
        if not present(getattr(target,field,None)) and present(value):
            setattr(target,field,int(value) if field in ('beds','baths') else value)
            changed=True
    # Preserve all sources even when they add provenance but no new fact.
    staged.raw_json=json.dumps(evidence,ensure_ascii=False)
    return None, changed


def accept(db, merged):
    from portals.listings import to_listing, _portal_sold_batch, _CARRIED_SOLD, _price_flag
    # Serialize automatic promoters with writes to the sold table on PostgreSQL.
    if db.bind.dialect.name=='postgresql':
        db.execute(text('LOCK TABLE properties_sold IN SHARE ROW EXCLUSIVE MODE'))
    existing={}
    for sale in db.query(PropertySold.address,PropertySold.suburb,PropertySold.sold_date,PropertySold.sale_price).yield_per(1000):
        existing.setdefault(identity({'address':sale.address,'suburb':sale.suburb,'sold_date':sale.sold_date}),[]).append(sale.sale_price)
    counts={'new':0,'skipped':0,'quarantined':0,'enriched':0}
    for item in merged:
        problem=reason(item)
        row=to_listing(item['source'],item,kind='sold') if problem in (None, 'Source evidence requires review') else None
        if row is not None and not problem:problem=_price_flag(db,row)
        key=identity(item)
        if not problem and key in existing:
            if all(p is not None and float(p)==float(item['sale_price']) for p in existing[key]):
                problem,changed=enrich_accepted(db,item)
                if not problem:
                    counts['enriched' if changed else 'skipped']+=1;continue
            else:problem='Disclosed price conflicts with an existing sale'
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
