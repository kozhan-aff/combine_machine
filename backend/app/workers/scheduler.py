"""Автопилот-воркер (APScheduler). Частый тик + throttle из конфига autonomy_settings.

Каждый тик читает конфиг СВЕЖИМ из БД -> тумблеры/интервал применяются без рестарта
воркера. Работу двигает orchestrator.run_sweep (single-flight внутри). Отдельный процесс
docker-compose `worker`, общий с панелью Postgres. Прежний суточный m1_cycle удалён —
его поведение = auto_discovery + auto_score через оркестратор.
"""
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

TICK_MIN = 5   # фиксированный частый тик; реальную частоту свипов задаёт sweep_interval_min


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
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(tick, "interval", minutes=TICK_MIN, id="autopilot_tick",
                  misfire_grace_time=TICK_MIN * 60)
    print(f"[worker] autopilot tick every {TICK_MIN} min (throttle from autonomy_settings)", flush=True)
    sched.start()


if __name__ == "__main__":
    main()
