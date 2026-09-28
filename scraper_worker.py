"""Dedicated collection/pricing worker; excludes unrelated legacy maintenance."""
import logging
import signal
import worker


def jobs():
    allowed={'new listings sweep','sold sweep','daily validated pricing'}
    from config import settings
    from portals.delisted import scheduled_run_once
    from portals.review_flow import resume_pending
    from portals.fill_worker import dispatch
    return [worker.Job('requested listing backfill', 60, dispatch,
                       enabled=lambda: settings.scraper_enabled)] + [job for job in worker.build_jobs() if job.name in allowed] + [
        worker.DurableJob('listing availability', 30 * 60, scheduled_run_once,
            enabled=lambda: settings.scraper_check_listings),
        worker.Job('resume private pricing', 60, resume_pending,
            enabled=lambda: settings.scraper_enabled)]


def main():
    logging.basicConfig(level='INFO')
    signal.signal(signal.SIGTERM,worker._handle_stop)
    signal.signal(signal.SIGINT,worker._handle_stop)
    return worker.run(jobs=jobs())


if __name__=='__main__':raise SystemExit(main())
