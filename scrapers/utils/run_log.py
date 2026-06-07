from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone


@dataclass(frozen=True)
class DomainRunStart:
    log_id: int
    domain: str
    mode: str
    run_date: str
    run_number: int
    configured_runs: int
    started_at: str


@dataclass(frozen=True)
class DomainRunUsage:
    domain: str
    mode: str
    run_date: str
    used_runs: int
    configured_runs: int


def _configured_runs_for_mode(domain_config, mode: str) -> int:
    if mode == 'HARVEST':
        return int(getattr(domain_config, 'harvest_runs_per_day', 0) or 0)
    if mode == 'EXTRACT':
        return int(getattr(domain_config, 'extract_runs_per_day', 0) or 0)
    raise ValueError(f'Unsupported run log mode: {mode!r}')


def begin_domain_run(domain_config, mode: str) -> DomainRunStart | None:
    """
    Create a STARTED run log if today's configured quota is not exhausted.

    The TargetDomain row is locked so multiple worker containers cannot reserve
    the same daily run slot concurrently.
    """
    from app_dashboard.models import DomainRunLog, TargetDomain

    mode = mode.upper()
    now = timezone.now()
    run_date = timezone.localdate(now)
    configured_runs = _configured_runs_for_mode(domain_config, mode)
    if configured_runs <= 0:
        return None

    with transaction.atomic():
        locked_domain = TargetDomain.objects.select_for_update().get(id=domain_config.id)
        configured_runs = _configured_runs_for_mode(locked_domain, mode)
        if configured_runs <= 0:
            return None

        completed_or_started_runs = DomainRunLog.objects.filter(
            domain=locked_domain.name,
            mode=mode,
            run_date=run_date,
        ).count()
        if completed_or_started_runs >= configured_runs:
            return None

        run_log = DomainRunLog.objects.create(
            domain=locked_domain.name,
            mode=mode,
            status='STARTED',
            run_date=run_date,
            run_number=completed_or_started_runs + 1,
            configured_runs=configured_runs,
            started_at=now,
        )

    return DomainRunStart(
        log_id=run_log.id,
        domain=run_log.domain,
        mode=run_log.mode,
        run_date=run_log.run_date.isoformat(),
        run_number=run_log.run_number,
        configured_runs=run_log.configured_runs,
        started_at=timezone.localtime(run_log.started_at).isoformat(),
    )


def get_domain_run_usage(domain_config, mode: str) -> DomainRunUsage:
    from app_dashboard.models import DomainRunLog

    mode = mode.upper()
    now = timezone.now()
    run_date = timezone.localdate(now)
    configured_runs = _configured_runs_for_mode(domain_config, mode)
    used_runs = DomainRunLog.objects.filter(
        domain=domain_config.name,
        mode=mode,
        run_date=run_date,
    ).count()
    return DomainRunUsage(
        domain=domain_config.name,
        mode=mode,
        run_date=run_date.isoformat(),
        used_runs=used_runs,
        configured_runs=configured_runs,
    )


def finish_domain_run(
    run_log_id: int,
    status: str,
    items_count: int = 0,
    error_message: str = '',
) -> None:
    from app_dashboard.models import DomainRunLog

    DomainRunLog.objects.filter(id=run_log_id).update(
        status=status.upper(),
        finished_at=timezone.now(),
        items_count=max(0, int(items_count or 0)),
        error_message=(error_message or '')[:5000],
    )
