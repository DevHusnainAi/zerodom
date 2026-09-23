"""The graph UI exists twice on disk and must stay byte-identical.

zerodom/graph_ui.{js,css} is canonical and ships in the wheel; the extension
needs its own copy because an MV3 popup can only load files from inside the
extension directory, and the wheel can't ship extension/. Neither constraint
is going away, so this test is the guard that keeps the copies from drifting
the way _DRIVING_UI_JS and popup.js used to.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PAIRS = [("graph_ui.js",), ("graph_ui.css",)]


@pytest.mark.parametrize("name", [p[0] for p in PAIRS])
def test_extension_copy_matches_the_package_copy(name):
    canonical = ROOT / "zerodom" / name
    mirror = ROOT / "extension" / name
    assert canonical.exists(), f"missing canonical {canonical}"
    assert mirror.exists(), f"missing mirror {mirror}"
    assert canonical.read_bytes() == mirror.read_bytes(), (
        f"{name} has drifted. Canonical is zerodom/{name}; resync with:\n"
        f"    cp zerodom/{name} extension/{name}"
    )


def test_graph_ui_ships_in_the_package():
    """Guards the wheel: a pip user's sidebar reads these off the installed
    package, so they must live under zerodom/, not only in extension/."""
    import zerodom

    pkg = Path(zerodom.__file__).parent
    for name in ("graph_ui.js", "graph_ui.css"):
        assert (pkg / name).exists(), f"{name} not importable from the package"
