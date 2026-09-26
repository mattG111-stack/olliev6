"""Dedicated collection/pricing worker; excludes unrelated legacy maintenance."""
import logging
import signal
import worker


def jobs():
    allowed={'new listings sweep','sold sweep','daily validated pricing'}
    from config import settings
    from portals.delisted import scheduled_run_once
    return [job for job in worker.build_jobs() if job.name in allowed] + [
        worker.DurableJob('listing availability', 30 * 60, scheduled_run_once,
            enabled=lambda: settings.scraper_check_listings)]


def main():
    logging.basicConfig(level='INFO')
    signal.signal(signal.SIGTERM,worker._handle_stop)
    signal.signal(signal.SIGINT,worker._handle_stop)
    return worker.run(jobs=jobs())


if __name__=='__main__':raise SystemExit(main())
