"""Party-invite accept/decline policy (`accept_invite`).

The trap this guards: ACCEPT and REJECT are two buttons in the *same* dialog and
the INVITED crop sits on ACCEPT, so "don't accept" is not "don't click" — an
undismissed invite dialog covers the lobby and strands the FSM.
"""

import pathlib

import pytest
import yaml

from wingman.config_schema import validate_config
from wingman.main import _invite_click_target

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CROPS = {"INVITED": object(), "REJECT": object(), "PLAY": object()}


def test_declining_clicks_reject():
    assert _invite_click_target(False, _CROPS) == "REJECT"


def test_accepting_clicks_the_invited_crop():
    """The INVITED crop is the ACCEPT button, which is why accepting reuses it."""
    assert _invite_click_target(True, _CROPS) == "INVITED"


@pytest.mark.parametrize("accept, missing", [(False, "REJECT"), (True, "INVITED")])
def test_missing_crop_yields_no_click_rather_than_the_other_button(accept, missing):
    """Falling back to the opposite button would silently invert the operator's
    decision — a worse failure than leaving the dialog up."""
    crops = {k: v for k, v in _CROPS.items() if k != missing}
    assert _invite_click_target(accept, crops) is None


def test_decline_never_resolves_to_the_accept_button():
    """Belt and braces on the direction that actually matters: with REJECT
    absent, declining must not degrade into accepting."""
    for crops in ({}, {"INVITED": object()}, {"PLAY": object()}):
        assert _invite_click_target(False, crops) != "INVITED"


# ---------------------------------------------------------------------------
# Shipped configuration
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_shipped_config_declines_by_default(shipped_cfg):
    assert shipped_cfg["accept_invite"] is False


def test_reject_crop_is_calibrated(shipped_cfg):
    """Declining is the default, so the crop it needs must actually exist —
    otherwise every invite takes the no-click branch."""
    reject = shipped_cfg["crops"]["REJECT"]
    (x1, y1), (x2, y2) = reject["coords"]
    assert 0.0 <= x1 < x2 <= 1.0
    assert 0.0 <= y1 < y2 <= 1.0


def test_accept_invite_is_declared_in_the_schema(shipped_cfg):
    """A key main.py reads must be in the schema or setting it fails startup."""
    cfg = dict(shipped_cfg, accept_invite=True)
    assert validate_config(cfg) == []
    assert any("accept_invite" in e for e in validate_config(dict(shipped_cfg, accept_invite="yes")))
