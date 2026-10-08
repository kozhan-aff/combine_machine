"""Автопилот-воркер (APScheduler). Частый тик + throttle из конфига autonomy_settings.

Каждый тик читает конфиг СВЕЖИМ из БД -> тумблеры/интервал применяются без рестарта
воркера. Работу двигает orchestrator.run_sweep (single-flight внутри). Отдельный процесс
docker-compose `worker`, общий с панелью Postgres. Прежний суточный m1_cycle удалён —
его поведение = auto_discovery + auto_score через оркестратор.
"""
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

TICK_MIN = 5   # фиксированный частый тик; реальную частоту свипов задаёт sweep_interval_min


def heartbeat() -> None:
    """Раз в минуту: «воркер жив» (F8-11) — виден в /diag и в docker healthcheck. Сбой записи не
    роняет планировщик: следующий удар повторит, а протухшее сердце честно покажет проблему."""
    try:
        from app.services import heartbeat as hb
        from app.services.autonomy import get_autonomy
        hb.beat(bool(get_autonomy()["autopilot_on"]))
    except Exception as e:  # noqa: BLE001
        print(f"[worker] heartbeat не записан: {type(e).__name__}", flush=True)


def reap_on_start() -> None:
    """Старт воркера = прошлый процесс мёртв: закрыть 'running'-журнал свипа (S7-20), если живого
    sweep в job_run нет (ручной свип из панели — в другом процессе — не трогаем)."""
    try:
        from app.services import jobs, orchestrator
        if not jobs.is_running("sweep"):
            n = orchestrator.reap_orphan_runs()
            if n:
                print(f"[worker] закрыто оборванных свипов в журнале: {n}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[worker] reap при старте не удался: {type(e).__name__}", flush=True)


def tick() -> None:
    from app.services import orchestrator
    from app.services.autonomy import get_autonomy

    cfg = get_autonomy()
    if not cfg["autopilot_on"]:
        return                                          # мастер выкл — применяется сразу
    last = orchestrator.last_finished_sweep_at()
    if last is not None:
        if (datetime.now(timezone.utc) - last).total_seconds() < cfg["sweep_interval_min"] * 60:
            return                                      # throttle: рано для следующего свипа
    orchestrator.run_sweep(trigger="cron")              # single-flight внутри


def main() -> None:
    reap_on_start()
    heartbeat()                                         # первый удар сразу, не через минуту
    sched = BlockingScheduler(timezone="UTC")
    from app.services.heartbeat import BEAT_SEC
    sched.add_job(heartbeat, "interval", seconds=BEAT_SEC, id="worker_heartbeat",
                  misfire_grace_time=BEAT_SEC)
    sched.add_job(tick, "interval", minutes=TICK_MIN, id="autopilot_tick",
                  misfire_grace_time=TICK_MIN * 60)
    print(f"[worker] autopilot tick every {TICK_MIN} min (throttle from autonomy_settings)", flush=True)
    sched.start()


if __name__ == "__main__":
    main()
