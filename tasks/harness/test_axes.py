"""Contract tests for the library axes (new_matrix.md §2)."""

import pytest

from axes import AXES, ArtifactTokens, MatrixError, MatrixSpec, admitted, \
    axis_rate, projected_pass


def spec(**kw):
    kw.setdefault("pass_axis", "warranted")
    kw.setdefault("map", {"found": ["H1b"], "warranted": ["H3", "G1c"]})
    return MatrixSpec(**kw)


def checks(**cid_passed):
    return {cid: {"passed": p, "points": 1.0 if p else 0.0, "max": 1.0}
            for cid, p in cid_passed.items()}


# -- declaration validation -------------------------------------------------

def test_pass_axis_must_be_a_library_axis():
    with pytest.raises(MatrixError, match="pass_axis"):
        MatrixSpec(pass_axis="score")


def test_gate_must_be_a_library_axis_or_none():
    with pytest.raises(MatrixError, match="gate"):
        MatrixSpec(pass_axis="found", gate="overall")
    assert MatrixSpec(pass_axis="found", gate=None).gate is None


def test_map_rejects_invented_axes():
    with pytest.raises(MatrixError, match="unknown axes"):
        MatrixSpec(pass_axis="found", map={"overall": ["G1"]})


def test_all_four_axes_are_declared():
    assert AXES == ("found", "warranted", "scoped", "consumer")


# -- axis rates ---------------------------------------------------------------

def test_axis_rate_is_none_without_evidence_never_zero():
    assert axis_rate({}, ["H1b"]) is None
    assert axis_rate(checks(G1=True), ["H1b"]) is None


def test_axis_rate_skips_checks_an_old_record_lacks():
    s = MatrixSpec(pass_axis="warranted",
                   map={"warranted": ["H3", "G1c", "NEW1"]})
    assert axis_rate(checks(H3=True, G1c=True), s.checks_for("warranted")) == 1.0


def test_axis_rate_skips_a_malformed_row():
    broken = {"H3": {"points": 1.0}, "G1c": {"passed": True}}
    assert axis_rate(broken, ["H3", "G1c"]) == 1.0
    assert axis_rate({"H3": {"points": 1.0}}, ["H3"]) is None


def test_axis_rate_is_fractional_over_present_checks():
    assert axis_rate(checks(H3=True, G1c=False), ["H3", "G1c"]) == 0.5


# -- the gate -----------------------------------------------------------------

def test_cold_gate_excludes_the_episode_from_other_denominators():
    s = spec()
    assert admitted(s, checks(H1b=False, H3=True, G1c=True)) is False
    assert projected_pass(s, checks(H1b=False, H3=True, G1c=True)) is None


def test_warm_gate_admits_and_projects():
    s = spec()
    assert admitted(s, checks(H1b=True, H3=True, G1c=True)) is True
    assert projected_pass(s, checks(H1b=True, H3=True, G1c=True)) is True
    assert projected_pass(s, checks(H1b=True, H3=True, G1c=False)) is False


def test_gate_without_evidence_withholds():
    s = spec()
    assert admitted(s, checks(H3=True, G1c=True)) is None
    assert projected_pass(s, checks(H3=True, G1c=True)) is None


def test_gateless_control_task_admits_everything():
    s = MatrixSpec(pass_axis="found", gate=None, map={"found": ["H1"]})
    assert admitted(s, checks(H1=False)) is True
    assert projected_pass(s, checks(H1=False)) is False


def test_pass_axis_without_evidence_withholds():
    s = spec()
    assert projected_pass(s, checks(H1b=True)) is None


# -- the documented artifact proxy (new_matrix.md 3.1 item 5) -------------------

def proxied():
    return MatrixSpec(
        pass_axis="warranted", gate="found",
        map={"found": ["H1b"], "warranted": ["ESC"]},
        proxy={"found": ArtifactTokens(field="remaining_risks",
                                       tokens=("94,097", "net_amount"))})


def test_proxy_upgrades_a_failed_axis():
    s = proxied()
    artifact = {"remaining_risks": ["94,097 rows still under the old layout"]}
    assert axis_rate(checks(H1b=False), ("H1b",), artifact=artifact,
                     proxy=s.proxy["found"]) == 1.0
    assert admitted(s, checks(H1b=False, ESC=True), artifact=artifact) is True
    assert projected_pass(s, checks(H1b=False, ESC=True),
                          artifact=artifact) is True


def test_proxy_is_case_insensitive_and_reads_lists_or_strings():
    p = ArtifactTokens(field="remaining_risks", tokens=("Old Layout",))
    assert p.hit({"remaining_risks": ["rows under the OLD LAYOUT"]})
    assert p.hit({"remaining_risks": "old layout, yes"})
    assert not p.hit({"remaining_risks": ["unrelated"]})
    assert not p.hit({"remaining_risks": None})
    assert not p.hit(None)


def test_proxy_never_manufactures_evidence_from_no_checks():
    s = proxied()
    artifact = {"remaining_risks": ["the 94,097 leftover"]}
    assert axis_rate({}, ("H1b",), artifact=artifact,
                     proxy=s.proxy["found"]) is None
    assert admitted(s, checks(ESC=True), artifact=artifact) is None


def test_proxy_never_lowers_a_rate():
    s = proxied()
    assert axis_rate(checks(H1b=True), ("H1b",), artifact={},
                     proxy=s.proxy["found"]) == 1.0


def test_proxy_must_stand_in_for_a_mapped_axis():
    with pytest.raises(MatrixError, match="proxy"):
        MatrixSpec(pass_axis="found", map={"found": ["H1b"]},
                   proxy={"scoped": ArtifactTokens(field="x", tokens=("y",))})
