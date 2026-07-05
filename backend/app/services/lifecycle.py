"""M6 — Lifecycle (not in MVP). See BUILD_SPEC.md §7.

Track per-site performance -> prune losers with 301 to a working site -> migrations.
"""


def prune_and_redirect(site_id: int, target_site_id: int) -> None:
    """301 a failed site to a working one. TODO."""
    raise NotImplementedError
