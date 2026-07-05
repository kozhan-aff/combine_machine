"""Scheduled jobs entrypoint (MVP: APScheduler). Runs the M1 loop periodically.

    discovery.run_discovery (pull drop feed) -> scoring.score_pending (enrich + score).
Purchase stays manual (gate). Runs inside the docker-compose `worker` service.
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from app.services import discovery, scoring


def m1_cycle() -> None:
    new = discovery.run_discovery()
    scored = scoring.score_pending()
    print(f"[worker] discovery +{new} new, scored {scored}", flush=True)


def main() -> None:
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(m1_cycle, "cron", hour=3, id="m1_cycle", misfire_grace_time=3600)
    print("[worker] M1 loop scheduled: discovery+scoring daily 03:00 UTC", flush=True)
    sched.start()


if __name__ == "__main__":
    main()
