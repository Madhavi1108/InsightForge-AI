"""InsightForge AI - scheduled-mode runner (Phase 35 / spec Phase 67,
part of ``docs/system-components.md``'s "Scheduler (new Phase 35)").

Behind ``run_pipeline.py --scheduler``. `APScheduler` is already pinned in
``requirements.txt`` but is an *optional* runtime dependency exactly like
Alteryx's engine or the Gemini SDK: it is imported lazily, inside
:func:`build_scheduler`, never at module import time, so ``import
src.scheduler`` always succeeds even when the package isn't installed
(this sandbox does not have it installed - confirmed while planning this
phase). A missing dependency is reported clearly, never silently faked.

``SCHEDULER_ENABLED`` (default ``false``, ``src.config.SchedulerSettings``)
gates everything: unset/false means ``run_scheduler()`` is a harmless no-op
(exit code 3) - the same family as ``scan()``'s "nothing to do" - rather
than a background loop nobody opted into.
"""
from __future__ import annotations

import logging

from src.config import SchedulerSettings, get_scheduler_settings

logger = logging.getLogger(__name__)


def build_scheduler(dispatch=None, settings: SchedulerSettings | None = None):
    """A configured (not yet started) APScheduler ``BlockingScheduler`` with
    one cron job calling ``dispatch()`` (default:
    :func:`src.orchestrator.scan`).

    Raises :class:`ModuleNotFoundError` if ``apscheduler`` isn't installed -
    callers (``run_scheduler``) catch this and report a clear message
    rather than pretending a scheduler is running.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    settings = settings if settings is not None else get_scheduler_settings()
    if dispatch is None:
        from src.orchestrator import scan
        dispatch = scan

    scheduler = BlockingScheduler()
    scheduler.add_job(dispatch, CronTrigger.from_crontab(settings.cron), id="insightforge_scan")
    return scheduler


def run_scheduler(settings: SchedulerSettings | None = None) -> int:
    """The implementation behind ``--scheduler``.

    Returns 3 (no code executed - same "nothing to do" family the rest of
    the orchestrator uses) when disabled or when APScheduler isn't
    installed; starts the blocking cron loop and returns 0 on a clean
    ``Ctrl+C``/SIGINT shutdown otherwise.
    """
    settings = settings if settings is not None else get_scheduler_settings()
    if not settings.enabled:
        print("[ok] scheduler disabled (SCHEDULER_ENABLED=false) - "
              "use --file / --scan / --watch, or set SCHEDULER_ENABLED=true")
        return 3

    try:
        scheduler = build_scheduler(settings=settings)
    except ModuleNotFoundError:
        print("[error] SCHEDULER_ENABLED=true but the 'apscheduler' package is not "
              "installed. Install requirements.txt into the project venv.")
        return 3

    print(f"[ok] scheduler starting - cron '{settings.cron}' (Ctrl+C to stop)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[ok] scheduler stopped")
        return 0
    return 0
