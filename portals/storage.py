"""Store source evidence before staging; an incomplete attempt never publishes."""
import json
from datetime import datetime, timezone
from models import PortalCollectionRun, PortalObservation


def collect_and_stage(db, *, sources, kind, cap):
    from portals.direct import collect, merge_records
    from portals.listings import to_listing, record
    from portals import checkpoints
    run=PortalCollectionRun(kind=kind,status='running')
    db.add(run);db.commit()
    run_id=run.id
    summary={};collected=[];progress={}
    try:
        for source in sources:
            progress[source]=checkpoints.load(db,source,kind)
            rows=collect(source,kind=kind,cap=cap,checkpoint=progress[source])
            for row in rows:
                db.add(PortalObservation(run_id=run_id,source=source,kind=kind,url=row['url'],
                    payload_json=json.dumps(row,ensure_ascii=False)))
            collected.extend(rows)
            summary[source]={'observed':len(rows)}
            run.summary_json=json.dumps(summary)
            db.commit()  # Finished sources survive a later source failure.
        merged=merge_records(collected)
        if kind=='sold':
            from portals.sold_acceptance import accept
            result=accept(db,merged)
            result.update(found=len(merged),run_id=run_id,excluded=result['quarantined'],refreshed=0)
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
                'refreshed':stats.get('refreshed',0)}
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
