"""The espeak data-path check.

Worth testing because the thing it guards cannot be caught at runtime: espeak
calls exit() from inside the phonemizer, so there is no exception to assert on
and no traceback to read. The check has to be right before the fact.
"""
import pytest

from webnovel_audio import espeak


def test_short_path_is_silent(monkeypatch):
    monkeypatch.setattr(espeak, "data_path", lambda: "/opt/x/espeak-ng-data")
    assert espeak.too_long() == ""


def test_missing_espeak_is_silent(monkeypatch):
    """No espeak installed is not this check's problem to report."""
    monkeypatch.setattr(espeak, "data_path", lambda: "")
    assert espeak.too_long() == ""


def test_deep_path_reports_the_length_and_the_symptom(monkeypatch):
    deep = "/" + "d" * 200 + "/espeak-ng-data"
    monkeypatch.setattr(espeak, "data_path", lambda: deep)
    warn = espeak.too_long()
    assert warn, "a 215-character data path must be rejected"
    # the number, so it is obvious how far over it is
    assert str(len(deep)) in warn
    assert deep in warn
    # and the symptom, since that error string is what a user will search for
    assert "/home/runner" in warn


def test_boundary_is_not_off_by_one(monkeypatch):
    at = "/" + "d" * (espeak.SAFE_LEN - 3) + "/x"      # exactly SAFE_LEN
    assert len(at) == espeak.SAFE_LEN
    monkeypatch.setattr(espeak, "data_path", lambda: at)
    assert espeak.too_long() == "", "at the limit is still fine"
    monkeypatch.setattr(espeak, "data_path", lambda: at + "y")
    assert espeak.too_long() != "", "one past the limit is rejected"


def test_require_usable_refuses_rather_than_warning(monkeypatch):
    """It must raise, not print: the caller is about to hand control to
    espeak, which exits the interpreter instead of raising."""
    monkeypatch.setattr(espeak, "data_path", lambda: "/" + "d" * 200 + "/x")
    with pytest.raises(SystemExit) as exc:
        espeak.require_usable("cannot render:")
    msg = str(exc.value)
    assert "cannot render:" in msg
    assert str(espeak.SAFE_LEN) in msg
    assert "re-run `uv sync`" in msg


def test_require_usable_is_silent_when_fine(monkeypatch):
    monkeypatch.setattr(espeak, "data_path", lambda: "/opt/x/espeak-ng-data")
    espeak.require_usable("cannot render:")        # must not raise


def test_probe_succeeds_on_a_healthy_install():
    """The authoritative check: phonemize for real, in a subprocess.

    A subprocess because a bad data path aborts the interpreter — the reason
    this cannot be a try/except around phonemize().
    """
    pytest.importorskip("kokoro_onnx")
    if not espeak.data_path():
        pytest.skip("espeakng-loader not installed")
    ok, detail = espeak.probe()
    assert ok, f"espeak could not phonemize here: {detail}"
    assert detail, "a successful probe should return the phonemes it produced"


def test_version_comes_from_the_soname_not_a_library_call():
    """Reading it must not initialize espeak — initializing is what aborts
    when the data path is the very thing we are about to check."""
    v = espeak.library_version()
    if v is None:
        pytest.skip("espeakng-loader not installed")
    assert isinstance(v, tuple) and all(isinstance(n, int) for n in v)
    # the symlink chain has libespeak-ng.so.1 as well; the full triple wins
    assert len(v) >= 2, f"expected a full version, got {v}"


def test_length_check_lifts_itself_on_a_fixed_espeak(monkeypatch):
    """After 1.52.0 the POSIX path buffer grows to PATH_MAX, so the limit
    stops applying. It must not outlive the bug and pin where the project
    is allowed to live."""
    deep = "/" + "d" * 300 + "/espeak-ng-data"
    monkeypatch.setattr(espeak, "data_path", lambda: deep)

    monkeypatch.setattr(espeak, "library_version", lambda: (1, 52, 0))
    assert espeak.too_long(), "1.52.0 still has the small buffer"

    monkeypatch.setattr(espeak, "library_version", lambda: (1, 52, 1))
    assert espeak.too_long() == "", "a patched release lifts the limit"
    monkeypatch.setattr(espeak, "library_version", lambda: (1, 53, 0))
    assert espeak.too_long() == ""

    # and an older one is still constrained
    monkeypatch.setattr(espeak, "library_version", lambda: (1, 51, 1))
    assert espeak.too_long()


def test_unknown_version_stays_cautious(monkeypatch):
    """If the version cannot be read, assume the buffer is still small —
    a spurious refusal is recoverable, a mute mid-render abort is not."""
    monkeypatch.setattr(espeak, "data_path", lambda: "/" + "d" * 300 + "/x")
    monkeypatch.setattr(espeak, "library_version", lambda: None)
    assert espeak.too_long()
