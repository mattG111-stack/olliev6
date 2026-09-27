"""Store source evidence before staging; an incomplete attempt never publishes."""
import json
from datetime import datetime, timezone, timedelta
from copy import deepcopy
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
        counts={key:value for key in ('new','refreshed','excluded','pending','discovery_pending','failed_sources','record_errors')
                if type(value:=merged.get(key)) is int and value>=0}
        observed=sum(item.get('observed',0) for source in ('oneroof','trademe','homes')
                     if isinstance(item:=summary.get(source),dict)
                     and type(item.get('observed')) is int and item['observed']>=0)
        result.append({'id':run.id,'kind':kind,
            'status':run.status if run.status in ('running','complete','partial','failed') else 'unknown',
            'started_at':run.started_at.isoformat() if run.started_at else None,
            'finished_at':run.finished_at.isoformat() if run.finished_at else None,
            'observed':observed,**counts})
    return result


def retain_missing_facts(previous, current):
    """A sparse repeat observation cannot erase same-property physical facts.

    Prices, sale status and listing copy always come from the new observation.
    Keep the original field timestamp so an old measurement is never presented
    as newly observed. The unmodified observations remain in PortalObservation.
    """
    from portals.page_data import match_key, present
    key=match_key(previous)
    if (not key or key!=match_key(current) or current.get('kind')!='for_sale'
            or previous.get('kind')!='for_sale'
            or any(not previous.get(k) or previous.get(k)!=current.get(k)
                   for k in ('source','url'))):
        return current
    try:
        old_time=datetime.fromisoformat(previous['scraped_at'].replace('Z','+00:00'))
        new_time=datetime.fromisoformat(current['scraped_at'].replace('Z','+00:00'))
        if old_time.tzinfo and new_time.tzinfo and new_time<old_time:
            return previous
    except (KeyError,TypeError,ValueError,AttributeError):
        pass
    fields=('beds','baths','carspaces','floor_area_m2','land_area_m2',
            'building_age','property_type','zoning','type_of_title')
    retained={k:previous[k] for k in fields
              if present(previous.get(k)) and not present(current.get(k))}
    if not retained:return current
    result=deepcopy(current)
    origins=result.setdefault('provenance',{})
    old_origins=previous.get('provenance') or {}
    evidence={}
    for field,value in retained.items():
        origin=old_origins.get(field) or {'source':previous['source'],
            'url':previous['url'],'collected_at':previous.get('scraped_at')}
        result[field]=deepcopy(value)
        origins[field]=deepcopy(origin)
        evidence[field]={'value':deepcopy(value),'provenance':deepcopy(origin)}
        if field in (previous.get('source_conflicts') or {}):
            result.setdefault('source_conflicts',{})[field]=deepcopy(previous['source_conflicts'][field])
    result['raw_source']={'current':result.get('raw_source'), 'retained_facts':evidence}
    return result


def with_pending_evidence(db, collected):
    """Join later-source facts to pending review evidence, never live records.

    Replace a source's older snapshot when that source is observed again.
    Retain other sources for the conservative identity/conflict merger.
    """
    from models import PortalListing
    from portals.complete import fillable_rows
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
    prior=(fillable_rows(db,'for_sale').filter(PortalListing.address_key.in_(keys))
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
            fresh=current.pop(identity,None)
            combined.append(retain_missing_facts(row,fresh) if fresh is not None else row)
            seen.add(identity)
    combined.extend(current.values())
    return combined


def collect_and_stage(db, *, sources, kind, cap):
    from portals.direct import collect, merge_records, CollectorUnavailable, failure_code
    from portals.listings import to_listing, record
    from portals import checkpoints
    run=PortalCollectionRun(kind=kind,status='running')
    db.add(run);db.commit()
    run_id=run.id
    summary={};collected=[];progress={};failed_sources=0;record_errors=0;successful_sources=0
    try:
        progress={source:checkpoints.load(db,source,kind) for source in sources}
        def unfinished(state):
            return (state.get('pending_urls') or state.get('source_retry_after') or
                    state.get('discovery_coverage',{}).get('complete') is False)
        resuming=any(unfinished(state) for state in progress.values())
        for source in sources:
            if resuming and not unfinished(progress[source]) and progress[source].get('last_completed_pass_at'):
                summary[source]={'observed':0,'already_complete':True}
                continue
            now=datetime.now(timezone.utc)
            original=deepcopy(progress[source])
            try:waiting=datetime.fromisoformat(original.get('source_retry_after',''))>now
            except (ValueError,TypeError):waiting=False
            if waiting:
                failed_sources+=1
                summary[source]={'observed':0,'status':'cooldown','error_type':original.get('source_error_type','CollectorUnavailable'),
                                 'reason_code':original.get('source_error_code','collector_unavailable')}
                continue
            try:
                rows=collect(source,kind=kind,cap=cap,checkpoint=progress[source],isolate_records=True)
            except CollectorUnavailable as exc:
                # A failed source must not advance its cursor or erase other
                # sources' valid work. Store no exception text/credentials.
                reason=failure_code(exc)
                original.update(source_retry_after=(now+timedelta(hours=1)).isoformat(),source_error_type='CollectorUnavailable',source_error_code=reason)
                progress[source]=original
                failed_sources+=1
                summary[source]={'observed':0,'status':'failed','error_type':'CollectorUnavailable','reason_code':reason}
                continue
            successful_sources+=1
            progress[source].pop('source_retry_after',None)
            progress[source].pop('source_error_type',None)
            progress[source].pop('source_error_code',None)
            for row in rows:
                db.add(PortalObservation(run_id=run_id,source=source,kind=kind,url=row['url'],
                    payload_json=json.dumps(row,ensure_ascii=False)))
            rejected=[row for row in rows if row.get('_collection_rejected')]
            record_errors+=len(rejected)
            collected.extend(row for row in rows if not row.get('_collection_rejected'))
            summary[source]={'observed':len(rows),'record_errors':len(rejected),'status':'complete'}
            run.summary_json=json.dumps(summary)
            db.commit()  # Finished sources survive a later source failure.
        # A Homes detail may arrive in a later bounded pass than OneRoof's
        # search result. Preserve that pending evidence across run boundaries.
        outcome='partial' if failed_sources and successful_sources else ('failed' if failed_sources else 'complete')
        merged=merge_records(with_pending_evidence(db,collected) if kind=='for_sale' else collected)
        if kind=='sold':
            from portals.sold_acceptance import accept
            result=accept(db,merged)
            result.update(found=len(merged),run_id=run_id,excluded=result['quarantined'],refreshed=result.get('enriched',0),
                          pending=sum(len(s.get('pending_urls',[])) for s in progress.values()),
                          discovery_pending=sum(s.get('discovery_coverage',{}).get('complete') is False for s in progress.values()))
            result['excluded']+=record_errors
            result.update(failed_sources=failed_sources,record_errors=record_errors)
            summary['merged']=result
            for source,state in progress.items():checkpoints.save(db,source,kind,state)
            run.status=outcome;run.finished_at=datetime.now(timezone.utc)
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
        result['excluded']+=record_errors
        result.update(failed_sources=failed_sources,record_errors=record_errors)
        summary['merged']=result
        for source,state in progress.items():checkpoints.save(db,source,kind,state)
        run.status=outcome;run.finished_at=datetime.now(timezone.utc)
        run.summary_json=json.dumps(summary);db.commit()
        return {'merged':result}
    except Exception as exc:
        db.rollback()
        run=db.get(PortalCollectionRun,run_id)
        run.status='failed';run.error_type=type(exc).__name__
        run.finished_at=datetime.now(timezone.utc);run.summary_json=json.dumps(summary)
        db.commit()
        raise
