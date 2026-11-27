"""Setting a drive up for the Wii, in one call - used by the app's Drive section and
``rewii setup``.

"Set up" = make ReWii's folders, record that in the drive's manifest, install the
essential apps of the starter kit (:data:`rewii.core.osc.STARTER_KIT`) and point
USB Loader GX at the online-play server chosen in Settings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import osc
from .config import Settings
from .fileops import explain_os_error
from .layout import DriveLayout
from .progress import Cancelled, CancelToken, ProgressFn

NEXT_STEPS = ("Next: on the Wii, back up the NAND, install Priiloader and the cIOS "
              "(System & Guides).")


@dataclass
class SetupReport:
    """What :func:`setup_drive` did."""
    kit: osc.StarterKitReport
    online_note: str = ""         # why the loader's online server wasn't set ("" = it was, or n/a)

    def lines(self) -> tuple[list[str], bool]:
        """Plain-language lines for the user and whether anything went wrong."""
        lines, bad = osc.kit_lines(self.kit)
        if self.online_note:
            lines.append(self.online_note)
        lines.append(NEXT_STEPS)
        return lines, bad


def planned_apps(settings: Settings) -> list[osc.StarterItem]:
    """The apps :func:`setup_drive` will install for the console in ``settings``."""
    return osc.starter_kit(settings.console_type, "essential")


def setup_drive(layout: DriveLayout, settings: Settings, progress: ProgressFn | None = None,
                cancel: CancelToken | None = None) -> SetupReport:
    """Set the drive up (see the module docstring). Raises ``DriveNotConnected`` if the
    drive isn't there; a failing app or loader setting is reported, not raised."""
    layout.ensure_dirs()
    layout.write_manifest({"created_by": "ReWii", "updated": time.strftime("%Y-%m-%d %H:%M")})
    kit = osc.install_starter_kit(layout, settings.console_type, "essential", progress, cancel)
    note = ""
    try:
        from .online import set_loader_private_server
        if layout.gx_config.parent.exists():
            set_loader_private_server(layout, settings.online_server)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - reported; the apps are installed anyway
        note = f"USB Loader GX's online server wasn't set: {explain_os_error(e, layout.root)}"
    return SetupReport(kit, note)
