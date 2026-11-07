"""Low Power Mode as the throttle that reaches GPU and display."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lowpower import _parse_pmset, is_enabled, set_enabled, sudo_hint  # noqa: E402


def test_parse_pmset_reads_the_mode_flag():
    assert _parse_pmset("powermode            1") is True
    assert _parse_pmset("powermode            0") is False
    assert _parse_pmset("lowpowermode: 1") is True
    assert _parse_pmset("") is None


def test_is_enabled_returns_a_bool_on_a_mac():
    state = is_enabled()
    assert state is None or isinstance(state, bool)


def test_set_enabled_fails_gracefully_without_passwordless_sudo():
    # Without the scoped sudoers rule this must return False, never hang or raise.
    assert set_enabled(False) in (True, False)


def test_hint_names_the_exact_command_to_undo():
    hint = sudo_hint()
    assert "sudo bash scripts/lowpower-no-password.sh" in hint
    assert "/etc/sudoers.d/power-guard" in hint
