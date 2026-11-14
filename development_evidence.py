"""Withhold development calculations when the parcel behind them is unverified."""
import math
import re


def issue(address, land, cv):
    if re.match(r"^\s*\d+[A-Za-z]?\s*/\s*\d", str(address or "")):
        return "Individual unit parcel area requires title verification"
    try:
        land, cv = float(land), float(cv)
        if math.isfinite(land) and math.isfinite(cv) and land > 0 and cv > 0 and cv/land < 150:
            return "Land area inconsistent with recorded council valuation; verify parcel"
    except (ValueError, TypeError):pass
    return None


def repair_stored_signals(db):
    """Idempotent evidence-preserving removal of unsafe *derived* signals.

    Addresses, source areas, prices, photos and listing visibility are retained.
    The previous derived fields and reason are saved as a review finding.
    """
    import json
    from sqlalchemy import or_
    from models import PropertyForSale as P, PortalFinding
    fields = ['sections','dwellings','max_addl_lots','gross_sales','total_subdivided_value',
              'subdivision_profit','best_net_gain','subdivision_premium','best_strategy']
    changed=0
    for p in db.query(P).filter(or_(P.can_subdivide.is_(True),P.is_subdividable.is_(True),P.best_net_gain.isnot(None))).yield_per(500):
        reason=issue(p.address,p.land_area_m2,p.cv_numeric)
        if not reason:continue
        previous={name:getattr(p,name,None) for name in fields}
        db.add(PortalFinding(property_id=p.id,batch_id=p.import_batch_id,source='quality_guard',
            field='development_screen',kind='fact',status='pending',value_text=reason,
            extra_json=json.dumps({'reason':reason,'previous_derived_fields':previous})))
        p.can_subdivide=False;p.is_subdividable=False
        for name in fields:setattr(p,name,None)
        changed+=1
    db.commit()
    return changed
