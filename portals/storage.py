"""Store source evidence before staging; an incomplete attempt never publishes."""
import json
from datetime import datetime, timezone
from models import PortalCollectionRun, PortalObservation


def latest_runs(db):
    """Report worker evidence without exposing raw snapshots or configuration."""
    result=[]
    for kind in ('for_sale','sold'):
        run=(db.query(PortalCollectionRun).filter_by(kind=kind)
             .order_by(PortalCollectionRun.id.desc()).first())
        if run is None:continue
        try:summary=json.loads(run.summary_json or '{}')
        except (ValueError,TypeError):summary={}
        if not isinstance(summary,dict):summary={}
        merged=summary.get('merged',{})
        if not isinstance(merged,dict):merged={}
        counts={key:value for key in ('new','refreshed','excluded','pending','discovery_pending')
                if type(value:=merged.get(key)) is int and value>=0}
        observed=sum(item.get('observed',0) for source in ('oneroof','trademe','homes')
                     if isinstance(item:=summary.get(source),dict)
                     and type(item.get('observed')) is int and item['observed']>=0)
        result.append({'id':run.id,'kind':kind,
            'status':run.status if run.status in ('running','complete','failed') else 'unknown',
            'started_at':run.started_at.isoformat() if run.started_at else None,
            'finished_at':run.finished_at.isoformat() if run.finished_at else None,
            'observed':observed,**counts})
    return result


def with_pending_evidence(db, collected):
    """Join later-source facts to pending review evidence, never live records.

    Replace a source's older snapshot when that source is observed again.
    Retain other sources for the conservative identity/conflict merger.
    """
    from models import PortalListing
    from portals.page_data import match_key
    from trademe import address_key
    if not collected:
        return collected
    def candidate(row):
        key=match_key({**row,'district':row.get('district') or 'unknown'})
        return (key[0],key[1],key[3]) if key and key[3] else None
    targets={key for row in collected if (key:=candidate(row))}
    keys={address_key(row.get('address'),row.get('suburb')) for row in collected}
    current={(row['source'],row['url']):row for row in collected}
    combined=[];seen=set()
    prior=(db.query(PortalListing).filter(PortalListing.kind=='for_sale',
        PortalListing.status=='pending',PortalListing.address_key.in_(keys))
        .order_by(PortalListing.id.desc()))
    for pending in prior:
        try:data=json.loads(pending.raw_json or '{}')
        except (ValueError,TypeError):continue
        if not isinstance(data,dict) or not data.get('_apex_direct'):continue
        snapshots=data.get('source_snapshots') or [data]
        for row in snapshots:
            if not isinstance(row,dict) or row.get('kind')!='for_sale' or candidate(row) not in targets:
                continue
            identity=(row.get('source'),row.get('url'))
            if not all(identity) or identity in seen:continue
            combined.append(current.pop(identity,row));seen.add(identity)
    combined.extend(current.values())
    return combined


def collect_and_stage(db, *, sources, kind, cap):
    from portals.direct import collect, merge_records
    from portals.listings import to_listing, record
    from portals import checkpoints
    run=PortalCollectionRun(kind=kind,status='running')
    db.add(run);db.commit()
    run_id=run.id
    summary={};collected=[];progress={}
    try:
        progress={source:checkpoints.load(db,source,kind) for source in sources}
        resuming=any(state.get('pending_urls') for state in progress.values())
        for source in sources:
            if resuming and not progress[source].get('pending_urls') and progress[source].get('last_completed_pass_at'):
                summary[source]={'observed':0,'already_complete':True}
                continue
            rows=collect(source,kind=kind,cap=cap,checkpoint=progress[source])
            for row in rows:
                db.add(PortalObservation(run_id=run_id,source=source,kind=kind,url=row['url'],
                    payload_json=json.dumps(row,ensure_ascii=False)))
            collected.extend(rows)
            summary[source]={'observed':len(rows)}
            run.summary_json=json.dumps(summary)
            db.commit()  # Finished sources survive a later source failure.
        # A Homes detail may arrive in a later bounded pass than OneRoof's
        # search result. Preserve that pending evidence across run boundaries.
        merged=merge_records(with_pending_evidence(db,collected) if kind=='for_sale' else collected)
        if kind=='sold':
            from portals.sold_acceptance import accept
            result=accept(db,merged)
            result.update(found=len(merged),run_id=run_id,excluded=result['quarantined'],refreshed=result.get('enriched',0),
                          pending=sum(len(s.get('pending_urls',[])) for s in progress.values()),
                          discovery_pending=sum(s.get('discovery_coverage',{}).get('complete') is False for s in progress.values()))
            summary['merged']=result
            for source,state in progress.items():checkpoints.save(db,source,kind,state)
            run.status='complete';run.finished_at=datetime.now(timezone.utc)
            run.summary_json=json.dumps(summary);db.commit()
            return {'merged':result}
        rows=[r for item in merged if (r:=to_listing(item['source'],item,kind=kind)) is not None]
        stats={}
        new,skipped=record(db,rows,refresh_pending=True,stats=stats,commit=False)
        result={'found':len(rows),'new':new,'skipped':skipped,
                'excluded':len(merged)-len(rows),'run_id':run_id,
                'refreshed':stats.get('refreshed',0),
                'pending':sum(len(s.get('pending_urls',[])) for s in progress.values()),
                'discovery_pending':sum(s.get('discovery_coverage',{}).get('complete') is False for s in progress.values())}
        summary['merged']=result
        for source,state in progress.items():checkpoints.save(db,source,kind,state)
        run.status='complete';run.finished_at=datetime.now(timezone.utc)
        run.summary_json=json.dumps(summary);db.commit()
        return {'merged':result}
    except Exception as exc:
        db.rollback()
        run=db.get(PortalCollectionRun,run_id)
        run.status='failed';run.error_type=type(exc).__name__
        run.finished_at=datetime.now(timezone.utc);run.summary_json=json.dumps(summary)
        db.commit()
        raise
