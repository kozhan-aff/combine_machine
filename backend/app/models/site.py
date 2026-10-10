"""Provisioned sites and their content pages. See BUILD_SPEC.md §5."""
from datetime import datetime
from sqlalchemy import String, Text, Boolean, Float, JSON, ForeignKey, DateTime, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id"))
    status: Mapped[str] = mapped_column(String(32), default="provisioning", index=True)
    # provisioning | content | published | monitoring | pruned

    cf_zone_id: Mapped[str | None] = mapped_column(String(64))
    # Cloudflare Control Center P0 (docs/superpowers/plans/2026-07-14-cloudflare-p0.md, задача 1):
    # заводятся ПУСТЫМИ — mirror-таблицы на момент миграции 0016 ещё не наблюдали ни одной зоны.
    # Backfill из legacy cf_zone_id делает cf_sync._backfill_site_links (задача 4), когда зоны уже
    # синхронизированы. cloudflare_account_id — ВНЕШНИЙ hex Cloudflare (см. models/cloudflare.py).
    cf_zone_mirror_id: Mapped[int | None] = mapped_column(ForeignKey("cloudflare_zone_mirrors.id"))
    cloudflare_account_id: Mapped[str | None] = mapped_column(String(64))
    origin_ip: Mapped[str | None] = mapped_column(String(64))
    aapanel_site_name: Mapped[str | None] = mapped_column(String(255))
    doc_root: Mapped[str | None] = mapped_column(String(512))

    # Последний отказ на шаге SSL провижна (смена режима на Cloudflare). Не блокирует сайт —
    # vhost работает и на 80 — но при origin-only-80 именно этот шаг решает, поедет ли HTTPS,
    # и раньше он молча глотался (`except Exception: pass`). NULL = SSL встал (или ещё не
    # пробовали); текст = провижн доехал, но HTTPS под вопросом. Показывается на /sites/{id}.
    ssl_error: Mapped[str | None] = mapped_column(Text)

    # M3: шаг провижна и состояние origin-HTTPS (миграция 0027). Режим SSL Cloudflare ВЫВОДИТСЯ из
    # origin_https, а не наоборот: full/strict без HTTPS-vhost+сертификата на origin даёт 525.
    #   origin_https: NULL/'none' — на origin нет подтверждённого HTTPS (CF 'flexible');
    #                 'ok'        — origin отвечает по HTTPS (сертификат не наш) -> CF 'full';
    #                 'origin_ca' — наш Cloudflare Origin CA поставлен в aaPanel и ответил -> 'strict'.
    #   provision_step: zone | await_ns | vhost | origin_tls | dns | verify | done — на каком шаге
    #                 остановился последний прогон (карточка сайта и свип показывают его словами).
    origin_https: Mapped[str | None] = mapped_column(String(16))
    provision_step: Mapped[str | None] = mapped_column(String(24))
    cf_name_servers: Mapped[list | None] = mapped_column(JSON)          # NS, которые поставить у регистратора
    ns_waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ns_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # последний activation_check

    niche: Mapped[str | None] = mapped_column(String(255))
    # Оффер сайта, привязанный ЯВНО (миграция 0028): о нём пишутся страницы и на него ведёт CTA.
    # Без него генерация отказывает — «самого раннего активного оффера портфеля» больше нет.
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))
    template: Mapped[str | None] = mapped_column(String(255))

    gsc_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sitemap_submitted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ротация провижна (F2.1)

    # Когда досье конкурентов собирали в последний раз, с любым исходом кроме отмены (миграция 0038).
    # От неё свип считает паузу перед повтором: по строкам site_research несобравшееся досье не видно
    # (пустое строк не имеет, а у протухшего после пустой пересборки остаются старые).
    research_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages: Mapped[list["Page"]] = relationship(back_populates="site")

    # ОДИН САЙТ НА ДОМЕН (миграция 0016). Гонка panel/worker (кнопка провижна против
    # автопилотного свипа — РАЗНЫЕ ПРОЦЕССЫ) могла завести два Site на один Domain так же,
    # как 0014 ловила дубли Page: SELECT «сайт уже есть» под READ COMMITTED не видит чужую
    # незакоммиченную строку. Cloudflare-привязка (cf_zone_mirror_id) обязана указывать на
    # ОДИН сайт домена, иначе backfill не знал бы, какой из дублей обновлять.
    __table_args__ = (Index("uq_site_per_domain", "domain_id", unique=True),)


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    url_path: Mapped[str] = mapped_column(String(512))
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="draft")   # draft | edited | published
    # HARD GATE: published only allowed from `edited`
    body: Mapped[str | None] = mapped_column(Text)

    # F26 (аудит 2026-07-14) + миграция 0018: язык и оффер, ПОД КОТОРЫЕ РЕАЛЬНО ПИСАЛАСЬ эта
    # страница. Раньше publish.py пересчитывал «текущий активный оффер сайта» ЗАНОВО в момент
    # публикации (`_pick_offer`, та же query, что и content.generate_site) — если набор
    # SiteOffer сайта поменялся между генерацией и публикацией (оператор добавил/сменил оффер
    # с меньшим Offer.id), страница, написанная про бренд A на языке en, публиковалась со
    # ссылкой на бренд B и `<html lang="ru">`. Пишет ТОЛЬКО content.generate_site, в момент
    # создания Page — это факт истории ("под что писали"), а не текущее состояние сайта.
    # NULL = legacy-страница старше 0018, для неё publish.py считает текущий активный оффер,
    # как раньше (нечего восстанавливать задним числом).
    lang: Mapped[str | None] = mapped_column(String(8))
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))

    index_status: Mapped[str] = mapped_column(String(32), default="unknown")  # unknown | indexed | not_indexed
    index_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Спека 4 (2026-07-18): advisory-оценка черновика вторым LLM-вызовом, ДО того как
    # человек открыл страницу. НЕ гейт — mark_edited работает независимо от этих полей.
    critic_score: Mapped[float | None] = mapped_column(Float)          # 0.0–1.0
    critic_notes: Mapped[dict | None] = mapped_column(JSON)            # {"pass", "issues", "code", "model", "round"}
    critic_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Спека 2026-10-10 §6.2: структурный ответ писателя (PageDoc как dict); body — его рендер.
    # blocks_stale=True — body правили руками в редакторе, рендер из blocks его не затирает.
    blocks: Mapped[dict | None] = mapped_column(JSON)
    blocks_stale: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Последняя попытка publish_site по этой edited-странице (любой исход). Ротация стадии publish
    # в оркестраторе: сайт, который не публикуется (нет оффера, домен не подтвердил запись, сбой
    # панели), уходит в хвост очереди и не занимает cap навсегда.
    publish_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped["Site"] = relationship(back_populates="pages")

    # ОДНА СТРАНИЦА НА ПУТЬ (миграция 0014). Не косметика: url_path — это ФАЙЛ на сервере
    # (`_target_path` в publish.py: docroot + url_path + /index.html). Две строки с одним путём
    # рендерятся В ОДИН И ТОТ ЖЕ файл, и опубликованной оказывается та, что записалась последней:
    # человек редактирует одну страницу, а в интернет уезжает другая — и панель показывает обе
    # как настоящие. Дубли заводились гонкой генерации (кнопка в панели против стадии generate
    # в воркере — РАЗНЫЕ ПРОЦЕССЫ; SELECT «страница уже есть» в content.generate_site их не
    # разводит: чужая незакоммиченная строка под READ COMMITTED невидима).
    __table_args__ = (Index("uq_page_per_path", "site_id", "url_path", unique=True),)
