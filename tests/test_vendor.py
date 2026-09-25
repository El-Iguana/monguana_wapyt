"""The vendored editor bundle is what its build recorded."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "appcode" / "vendor" / "codemirror"


def test_bundle_matches_its_recorded_checksum():
    """
    A hand edit to the minified file, or a rebuilt one with a stale VERSION,
    fails here. Rebuild with tools/codemirror/build.sh, which writes both.
    """
    recorded = re.search(r"^sha256 ([0-9a-f]{64})$", (VENDOR / "VERSION").read_text(), re.M)
    assert recorded, "VERSION has no sha256 line"
    actual = hashlib.sha256((VENDOR / "monguana-editor.js").read_bytes()).hexdigest()
    assert actual == recorded.group(1)


def test_bundle_stays_inside_the_csp():
    """No eval, no workers, no remote loads: pytincture's CSP allows 'self' only."""
    source = (VENDOR / "monguana-editor.js").read_text()
    for construct in ("new Function", "eval(", "new Worker", "importScripts"):
        assert construct not in source, construct
    remote = set(re.findall(r"https?://[\w.-]+", source)) - {"http://www.w3.org"}
    assert not remote, remote
