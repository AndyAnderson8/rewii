"""Every id that links a finding to an action, a guide or an app list resolves to something real.

A typo in one of these chains used to fail silently (the button did nothing)."""
from __future__ import annotations

import re
from pathlib import Path

import rewii.core.health as health
import rewii.core.syscheck as syscheck
from rewii.core import guides, osc
from rewii.core.findings import SAFE_FIXES, Finding, Fix, Severity
from rewii.core.wad import Risk
from rewii.tui.base import SEVERITY_ICON, severity_icon
from rewii.tui.panes import drive as drive_pane
from rewii.tui.panes import system as system_pane


def _fixes_used(module) -> set[Fix]:
    """The ``Fix.X`` members a module's source mentions (what its findings can name)."""
    names = set(re.findall(r"\bFix\.([A-Z0-9_]+)", Path(module.__file__).read_text("utf-8")))
    return {Fix[n] for n in names}          # KeyError = a fix id that doesn't exist


def test_one_severity_type_everywhere():
    f = Finding("x", "warning", "m")                        # type: ignore[arg-type]
    assert f.severity is Severity.WARNING and f.severity == "warning" and f.fix == ""
    assert health.WARN is Severity.WARNING and Severity.WARNING.short == "warn"
    assert Risk("error", "c", "m").blocks and Risk("warning", "c", "m").severity is Severity.WARNING  # type: ignore[arg-type]
    assert set(SEVERITY_ICON) == set(Severity)
    assert severity_icon("warning") == "!" and severity_icon("nonsense") == "?"
    assert [x.severity for x in sorted([Finding("a", Severity.INFO, ""), Finding("b", Severity.ERROR, "")],
                                       key=lambda x: x.severity.rank)] == ["error", "info"]


def test_every_health_fix_is_automatic_or_has_a_ui_action():
    used = _fixes_used(health)
    assert used, "health.py no longer names its fixes with Fix.X"
    assert set(health.FIXERS) == set(SAFE_FIXES) == set(health.SAFE_FIXES)
    for fix in used:
        assert fix in SAFE_FIXES or fix in drive_pane.FIX_LABELS, f"{fix} has no handler"
    assert not set(drive_pane.FIX_LABELS) & set(SAFE_FIXES)


def test_every_syscheck_fix_leads_to_a_guide():
    used = _fixes_used(syscheck)
    assert used == set(system_pane.SYSCHECK_FIX_GUIDE)
    known = {g.id for g in guides.GUIDES}
    for fix, guide_id in system_pane.SYSCHECK_FIX_GUIDE.items():
        assert guide_id in known, f"{fix} points at a guide that doesn't exist: {guide_id}"
        assert guides.get(guide_id).id == guide_id


def test_every_guide_prepare_action_exists_and_its_apps_are_in_the_starter_kit():
    kit = {i.slug for i in osc.STARTER_KIT}
    for g in guides.GUIDES:
        if not g.prepare:
            continue
        assert g.prepare in system_pane.PREPARE_APPS or g.prepare in system_pane.PREPARE_ACTIONS, \
            f"guide {g.id!r}: nothing prepares {g.prepare!r}"
    for method in system_pane.PREPARE_ACTIONS.values():
        assert callable(getattr(system_pane.SystemPane, method))
    for action, slugs in system_pane.PREPARE_APPS.items():
        assert slugs and set(slugs) <= kit, f"{action}: {set(slugs) - kit} not in osc.STARTER_KIT"
    used = {g.prepare for g in guides.GUIDES if g.prepare}
    assert set(system_pane.PREPARE_APPS) | set(system_pane.PREPARE_ACTIONS) == used    # nothing dead either
