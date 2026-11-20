"""Orchestration d'une exécution : collecte, dédoublonnage, filtres, stockage.

Les logs n'affichent que des compteurs : jamais d'intitulé, d'entreprise ni de critère.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vigie.radar import sources as source_registry
from vigie.radar.config import RadarConfig
from vigie.radar.dedup import Outcome, classify
from vigie.radar.filters import FilterSet
from vigie.radar.models import RunReport, SourceReport
from vigie.radar.sources import SourceFactory
from vigie.radar.sources.base import Source
from vigie.radar.store import Store
from vigie.settings import Settings

logger = logging.getLogger(__name__)

# Source du registre -> fonctionnalité de ``settings`` dont elle dépend.
SOURCE_FEATURE = {"adzuna": "adzuna", "france_travail": "france_travail"}

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_ALL_FAILED = 2
EXIT_SOME_FAILED = 3


@dataclass
class SourcePlan:
    """Ce qui sera réellement exécuté, et pourquoi le reste ne l'est pas."""

    active: list[str] = field(default_factory=list)
    pending_access: list[str] = field(default_factory=list)  # France Travail sans identifiants
    not_implemented: list[str] = field(default_factory=list)
    missing_variables: list[str] = field(default_factory=list)


class UnknownSourceSelectionError(Exception):
    """``--source`` désigne une source absente ou désactivée."""


def plan_sources(
    config: RadarConfig,
    settings: Settings,
    registry: dict[str, SourceFactory] | None = None,
    only: str | None = None,
) -> SourcePlan:
    registry = source_registry.REGISTRY if registry is None else registry
    enabled = config.sources.enabled()
    if only is not None:
        if only not in enabled:
            raise UnknownSourceSelectionError("source inconnue ou désactivée dans la configuration")
        enabled = [only]
    plan = SourcePlan()
    for name in enabled:
        if name not in registry:
            plan.not_implemented.append(name)
            continue
        feature = SOURCE_FEATURE.get(name)
        missing = settings.missing([feature]) if feature else []
        if name == "france_travail" and missing:
            plan.pending_access.append(name)
        elif missing:
            plan.missing_variables.extend(v for v in missing if v not in plan.missing_variables)
        else:
            plan.active.append(name)
    return plan


@dataclass
class RunResult:
    report: RunReport
    pending: int
    failed: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.failed:
            return "success"
        return "failed" if len(self.failed) == len(self.report.sources) else "partial"

    @property
    def exit_code(self) -> int:
        return {"success": EXIT_OK, "partial": EXIT_SOME_FAILED, "failed": EXIT_ALL_FAILED}[
            self.status
        ]


def default_since(config: RadarConfig, store: Store, now: datetime) -> datetime:
    """Dernière exécution réussie moins 1 jour de marge ; à défaut, l'ancienneté maximale."""
    last = store.last_success_started_at()
    if last is not None:
        return last - timedelta(days=1)
    return now - timedelta(days=max(query.max_age_days for query in config.searches))


def run_pipeline(
    config: RadarConfig,
    settings: Settings,
    store: Store,
    plan: SourcePlan,
    *,
    since: datetime | None = None,
    registry: dict[str, SourceFactory] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> RunResult:
    registry = source_registry.REGISTRY if registry is None else registry
    started = now()
    since = since or default_since(config, store, started)
    run_id = uuid.uuid4().hex
    store.start_run(run_id, started)
    filters = FilterSet(config.filters)
    reports: list[SourceReport] = []

    for name in plan.active:
        report = SourceReport(source=name)
        began = monotonic()
        try:
            source: Source = registry[name](settings, config)
            _collect(source, config, store, filters, report, since, now)
            report.errors += int(getattr(source, "skipped", 0))
        except Exception as exc:
            # Une source qui échoue n'interrompt pas les autres : on garde le type d'erreur.
            report.errors += 1
            report.error_message = f"{type(exc).__name__}: {exc}"
        report.duration_ms = int((monotonic() - began) * 1000)
        store.add_run_source(run_id, report)
        store.commit()
        reports.append(report)
        logger.info(
            "source=%s fetched=%d new=%d duplicates=%d filtered=%d errors=%d%s",
            name,
            report.fetched,
            report.new,
            report.duplicates,
            report.filtered,
            report.errors,
            " FAILED" if report.error_message else "",
        )

    finished = now()
    run_report = RunReport(run_id=run_id, started_at=started, finished_at=finished, sources=reports)
    result = RunResult(
        report=run_report,
        pending=0,
        failed=[r.source for r in reports if r.error_message],
    )
    store.finish_run(run_id, finished, result.status)
    store.commit()
    result.pending = store.count_pending()
    return result


def _collect(
    source: Source,
    config: RadarConfig,
    store: Store,
    filters: FilterSet,
    report: SourceReport,
    since: datetime,
    now: Callable[[], datetime],
) -> None:
    for query in config.searches:
        for offer in source.fetch(query, since):
            report.fetched += 1
            outcome, canonical = classify(store, offer)
            if outcome is Outcome.SEEN:
                store.touch(offer.uid, now())
                report.duplicates += 1
            elif outcome is Outcome.ALIAS and canonical is not None:
                store.add_alias(offer.uid, canonical)
                report.duplicates += 1
            else:
                reason = filters.reason(offer, query, now())
                store.insert_offer(offer, now(), filtered_reason=reason)
                if reason:
                    report.filtered += 1
                else:
                    report.new += 1
