"""Dedicated collection/pricing worker; excludes unrelated legacy maintenance."""
import logging
import signal
import worker


def jobs():
    allowed={'new listings sweep','sold sweep','daily validated pricing'}
    return [job for job in worker.build_jobs() if job.name in allowed]


def main():
    logging.basicConfig(level='INFO')
    signal.signal(signal.SIGTERM,worker._handle_stop)
    signal.signal(signal.SIGINT,worker._handle_stop)
    return worker.run(jobs=jobs())


if __name__=='__main__':raise SystemExit(main())
