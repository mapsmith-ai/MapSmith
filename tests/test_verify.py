"""Deterministic verification: checks recorded in manifests, critical failures raise."""

import json

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from conftest import (
    _EXTENSION_KEY,
    _SETTING_NAME,
    _spec_crs_keys,
    _spec_problems,
)
from mapsmith import verify
from mapsmith.engines import vector
from mapsmith.grid import DERIVED_ENVIRONMENT
from mapsmith.provenance import INPUTS_REPROJECTED


@pytest.fixture()
def points_gpkg(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(9.19, 45.46), Point(9.20, 45.47)],
        crs="EPSG:4326",
    )
    path = tmp_path / "points.gpkg"
    gdf.to_file(path)
    return path


def test_buffer_manifest_contains_passed_verification(points_gpkg, tmp_path):
    out = tmp_path / "buffered.gpkg"
    result = vector.buffer(str(points_gpkg), 250.0, str(out))
    assert result["verified"] is True
    manifest = json.loads((tmp_path / "buffered.gpkg.provenance.json").read_text())
    names = {c["name"] for c in manifest["verification"]}
    assert {"crs_present", "crs_matches", "geometry_valid", "feature_count_exact"} <= names
    assert all(c["passed"] for c in manifest["verification"])


def test_invalid_geometry_fails_critical_check(tmp_path):
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])  # self-intersecting
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:4326")
    path = tmp_path / "bowtie.gpkg"
    gdf.to_file(path)
    checks = verify.verify_vector_output(str(path))
    failed = [c for c in checks if c.name == "geometry_valid" and not c.passed]
    assert failed, "the invalid geometry must be detected"
    with pytest.raises(verify.VerificationError, match="geometry_valid"):
        verify.enforce(checks, "test_op")


def test_count_mismatch_raises(points_gpkg):
    checks = verify.verify_vector_output(str(points_gpkg), expect_count=99)
    with pytest.raises(verify.VerificationError, match="feature_count_exact"):
        verify.enforce(checks, "test_op")


def test_non_critical_failure_does_not_raise(points_gpkg):
    # extent check is non-critical: a failing bounds check alone must not raise
    checks = verify.verify_vector_output(
        str(points_gpkg), within_bounds=(0.0, 0.0, 0.1, 0.1)
    )
    extent = [c for c in checks if c.name == "extent_within_expected"]
    assert extent and not extent[0].passed
    verify.enforce(checks, "test_op")  # no exception


def test_reproject_verifies_target_crs(points_gpkg, tmp_path):
    out = tmp_path / "utm.gpkg"
    result = vector.reproject(str(points_gpkg), "EPSG:32632", str(out))
    assert result["verified"] is True
    manifest = json.loads((tmp_path / "utm.gpkg.provenance.json").read_text())
    crs_checks = [c for c in manifest["verification"] if c["name"] == "crs_matches"]
    assert crs_checks and crs_checks[0]["passed"]


def test_a_manifest_path_does_not_depend_on_the_host(tmp_path):
    """Two correct manifests for the same run must differ in no field but time.

    `str(path)` recorded the host's separator, so the same operation on the same
    bytes produced a backslash path on Windows and a forward-slash one elsewhere
    — a difference that describes nothing about the computation, and one that
    gives any consumer keying on the path two entries for one file (#30).

    The assertion is written with a path this host BUILT, not with a literal,
    and that is the whole subtlety. A first version asserted that a literal
    Windows string normalises everywhere, and CI on Linux disagreed — correctly:
    there, a backslash is a legal character in a filename, so rewriting one
    would corrupt a real path. Normalisation happens on the host that has
    separators, which is the only host that can produce the problem.
    """
    from pathlib import PurePath, PureWindowsPath

    from mapsmith.provenance import InputRecord, posix_path

    assert posix_path(PurePath("data") / "wells.gpkg") == "data/wells.gpkg"
    assert posix_path(PurePath("/srv/data") / "dem.tif").endswith("/srv/data/dem.tif")
    # The Windows flavour explicitly, so the behaviour is pinned from any host.
    assert PureWindowsPath(r"data\wells.gpkg").as_posix() == "data/wells.gpkg"
    assert PureWindowsPath(r"C:\work\dem.tif").as_posix() == "C:/work/dem.tif"
    # Only the separator: rewriting an absolute path to a relative one, or the
    # reverse, would misstate what actually ran.
    assert posix_path("data/wells.gpkg") == "data/wells.gpkg"

    source = tmp_path / "wells.gpkg"
    source.write_bytes(b"not really a geopackage")
    recorded = InputRecord.from_path(source)
    assert recorded.path == PurePath(source).as_posix()



def test_every_manifest_mapsmith_writes_conforms_to_the_spec(tmp_path):
    """MapSmith is an implementation of the manifest spec, not its definition.

    The day our manifest stops passing our own published validator, CI says so
    here — instead of a reader finding out. The validator is a vendored copy of
    the spec repository's stdlib-only implementation; the record under test is
    a REAL one, written by a real writer on a real file, because a hand-built
    record would validate the test's idea of a manifest rather than MapSmith's.
    """
    import json

    import geopandas as gpd

    from mapsmith.engines import vector

    source = tmp_path / "wells.parquet"
    gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=gpd.GeoSeries.from_wkt(["POINT (500000 5000000)", "POINT (500100 5000100)"]),
        crs="EPSG:32632",
    ).to_parquet(source)
    out = tmp_path / "wells_100m.parquet"
    vector.buffer(str(source), 100.0, str(out))

    record = json.loads((tmp_path / "wells_100m.parquet.provenance.json")
                        .read_text(encoding="utf-8"))
    assert _spec_problems(record) == [], "a MapSmith manifest no longer conforms to the spec"
    assert record["spec_version"].startswith("1.")

    # The record must describe the bytes it sits beside — recomputed here from
    # the file, not trusted from the record.
    import hashlib

    assert record["output"]["path"].endswith("wells_100m.parquet")
    assert "\\" not in record["output"]["path"]
    assert record["output"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_every_writing_operation_conforms_to_the_spec(tmp_path):
    """The test above proves one operation conforms. This one proves they all do.

    The catalog is the list, so an operation added tomorrow is covered the day
    it is added — which is the point: the previous version of this file
    validated `buffer_layer` and nothing else, and four raster operations
    shipped without anyone checking whether their manifests were still
    manifests. They were, and that was luck rather than a control.

    Operations behind an absent extra are skipped rather than silently passed,
    and the test says how many it actually validated: a conformance test that
    quietly checks nothing is worse than no conformance test.
    """
    import json
    from pathlib import Path

    from mapsmith import catalog
    from mapsmith.plans.registry import BINDINGS

    fixtures = _spec_fixtures(tmp_path)
    spec_crs_keys = _spec_crs_keys()
    writing = [
        entry["name"]
        for entry in catalog.OPERATIONS
        if entry["status"] == "available"
        and (binding := BINDINGS.get(entry["name"])) is not None
        and binding.output_arg is not None
    ]
    validated: list[str] = []
    skipped: list[str] = []
    for entry in catalog.OPERATIONS:
        if entry["status"] != "available":
            continue
        name = entry["name"]
        binding = BINDINGS.get(name)
        if binding is None or binding.output_arg is None:
            continue  # readers write no manifest
        call = fixtures.get(name)
        if call is None:
            skipped.append(name)
            continue
        try:
            result = call()
        except ImportError:
            skipped.append(name)
            continue
        record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
        assert _spec_problems(record) == [], f"{name} writes a manifest the spec rejects"
        assert record["operation"] == name
        assert record["spec_version"].startswith("1."), name
        # No shipped operation may reach the fallback. `write_for` appends a
        # failed `verification_present` check when a record would otherwise
        # carry none, because a dataset with a non-conforming manifest beside
        # it is worse than one with an honest "nobody looked". That net exists
        # for the seventeen writers that build their record by hand and never
        # pass through `verify.audited` — and a net nobody watches becomes a
        # place to land. This is the watching: if a real operation ever writes
        # a manifest whose only content is that nothing was checked, the sweep
        # says so here instead of the manifest saying it to a stranger.
        assert verify.VERIFICATION_ABSENT not in [
            c["name"] for c in record["verification"]
        ], (
            f"{name} verified nothing and only the fallback in write_for kept its "
            "manifest conforming. The fallback is a net, not a licence: give the "
            "operation at least one real check."
        )
        # The other half of the `crs_decisions` rule. The AST sweep reads the
        # keys written out at the 68 sites, including the branches no fixture
        # takes; this reads the keys that are actually in the record, including
        # the ones a merged dict put there -- `grid.describe` contributes two
        # that no literal at a call site shows. Neither sees what the other
        # sees, and the drift they caught on 2026-09-06 was invisible to both
        # until one of them existed.
        for key in record.get("crs_decisions", {}):
            # The SAME predicate the AST sweep uses, imported rather than
            # restated. The first version wrote `key.startswith("x-mapsmith:")`
            # here and used the section 3.6 regex there, so the two halves of
            # one rule disagreed: this one accepted `x-mapsmith:Bad Name!` and
            # that one did not.
            assert key in spec_crs_keys or _EXTENSION_KEY.fullmatch(key), (
                f"{name} wrote `crs_decisions.{key}`, which is neither a key section "
                "3.7 recommends nor a MapSmith extension `x-mapsmith:<name>` (D-077)."
            )
        # Saying an input was reprojected and not saying how is the silence this
        # whole line of work is about: across two datums with no grid installed
        # it is tens of metres, and `is_ballpark` is the boolean a consumer
        # branches on. The AST ratchet in `test_datum` reads the source and can
        # be satisfied by the word "transformation" appearing anywhere in the
        # function; this reads the record, so the two together mean the word has
        # to be there AND has to have put something in the manifest.
        # And the object next door, under the same rule with one difference:
        # section 3.8 asks for the configuration AS THE ENGINE REPORTS IT, so a
        # real setting name stays exactly as the engine spells it and only our
        # own readings carry the prefix. UPPER_SNAKE is the shape of a setting
        # -- `PROJ_NETWORK`, `GDAL_PAM_ENABLED`, `AREA_OR_POINT`, all three
        # named by the specification itself -- and it is derived from the key
        # rather than checked against a list of variables somebody keeps
        # filling in.
        for key in record.get("environment", {}):
            assert _SETTING_NAME.fullmatch(key) or _EXTENSION_KEY.fullmatch(key), (
                f"{name} wrote `environment.{key}`, which is neither a setting "
                "named the way an engine names one nor a MapSmith reading "
                "prefixed `x-mapsmith:` (D-077)."
            )
            # And DECLARED, for the reason `CRS_EXTENSIONS` is declared: the
            # prefix says a key is ours and cannot say it is not a synonym.
            # `AREA_OR_POINT` is named by section 3.8 itself, so
            # `x-mapsmith:area_or_point` passes the shape rule above and is
            # precisely the mistake that produced D-077 -- one object over. The
            # ratchet cannot judge synonymy; it makes whoever adds a key write
            # the answer down next to the declaration.
            if key.startswith("x-mapsmith:"):
                assert key.removeprefix("x-mapsmith:") in DERIVED_ENVIRONMENT, (
                    f"{name} wrote `environment.{key}`, which is ours and "
                    "undeclared. Add it to `grid.DERIVED_ENVIRONMENT` with the "
                    "sentence saying what it claims -- and why a setting the "
                    "engine already reports does not say it."
                )
        decisions = record.get("crs_decisions", {})
        if INPUTS_REPROJECTED in decisions:
            shift = decisions.get("transformation")
            assert isinstance(shift, dict) and isinstance(shift.get("is_ballpark"), bool), (
                f"{name} recorded that an input was reprojected and did not record "
                f"how: {decisions.get('transformation')!r}. Wire the call site to "
                "`datum.default_operation` where the engine chooses, or "
                "`datum.best_operation` where we do."
            )
        validated.append(name)

    # An invariant, not a threshold. `>= 32` was wrong in both directions: with
    # every extra installed there are 34 writing operations and 34 fixtures, so
    # the number let TWO fixtures disappear in silence -- exactly the defect
    # this test exists to close. And on a checkout without [raster]/[whitebox]
    # only 21 fixtures can run, so the same number FAILED while blaming missing
    # fixtures for an absent extra. Comparing the two sets says the true thing
    # in either environment, and needs no maintenance when the 35th operation
    # lands.
    assert set(validated) | set(skipped) == set(writing), (
        "every writing operation must be either validated or explicitly skipped; "
        f"unaccounted for: {sorted(set(writing) - set(validated) - set(skipped))}"
    )
    assert validated, "the sweep validated nothing at all"
    print(
        f"conformance sweep: {len(validated)} of {len(writing)} writing operations "
        f"validated, {len(skipped)} skipped for a missing extra ({sorted(skipped)})"
    )



def test_a_hand_built_record_cannot_write_a_manifest_the_spec_rejects(tmp_path):
    """The seventeen writers that never reach `verify.audited`.

    `audited` learned on 2026-09-05 to record an absent verification instead of
    raising and leaving the dataset orphaned. Seventeen writers of fifty-seven
    build their record by hand — `run_sql`, six in `raster.py`,
    `sedona_engine.spatial_join`, nine in `whitebox_engine` — and they reach
    none of that. Reproduced the day after: their exact shape wrote
    `verification: []`, which BOTH implementations reject, and
    `verify.enforce([])` does not raise, so the caller got `success` beside a
    manifest that is not a manifest. Strictly worse than the case just fixed,
    which was at least loud.

    The net is in `write_for` because that is the one place every manifest
    becomes a file. It appends rather than raising on purpose: raising there
    would leave the dataset with no record at all, which is the defect one
    level down and the reason the whole pair of fixes exists.

    This test walks the hand-built path exactly as those writers do — no
    `audited`, no `enforce` — and asserts the record that lands on disk is
    conforming, says which operation failed to check anything, and does not
    pretend the absence was a pass.
    """
    from mapsmith.provenance import ProvenanceRecord

    out = tmp_path / "hand_built.parquet"
    out.write_bytes(b"")
    record = ProvenanceRecord(
        operation="hand_built_writer",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    # Deliberately the shape of a writer that forgot: nothing added, and
    # `enforce` on an empty list, which does not raise and never did.
    verify.enforce([], "hand_built_writer")
    written = record.finish().write_for(str(out))

    manifest = json.loads(written.read_text(encoding="utf-8"))
    assert _spec_problems(manifest) == [], _spec_problems(manifest)
    absent = [c for c in manifest["verification"] if c["name"] == verify.VERIFICATION_ABSENT]
    assert len(absent) == 1, manifest["verification"]
    assert absent[0]["passed"] is False
    assert "hand_built_writer" in absent[0]["detail"]


def _check_names_in_source() -> tuple[dict[str, str], list[str]]:
    """Every `Check` name written in the source, and the ones it cannot read.

    Extracted on 2026-09-06 when a second caller appeared: the generator behind
    `docs/manifest-vocabulary.md` reads the same set to LIST it, and a test
    compares the two. Two copies of one reading is how a rule quietly becomes
    two rules -- this repository has watched that happen to a validator and its
    schema, which had drifted on every recommended field.
    """
    import ast
    from pathlib import Path

    import mapsmith

    root = Path(mapsmith.__file__).parent
    found: dict[str, str] = {}
    dynamic: list[str] = []
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        # Module-level tables of literal check names, so a lookup at the call
        # site is still statically readable. Only all-string dicts count.
        tables: dict[str, list[str]] = {}
        # And module-level constants holding one literal name. Same reasoning as
        # the tables, learned the hard way on 2026-09-05: moving a check name
        # out of the call and into `VERIFICATION_ABSENT` — because two distant
        # points had to agree on it — made it invisible to this sweep, and this
        # test went red. Red was the right answer and the fix is here, not
        # there: the alternative is that extracting a constant, which is the
        # normal thing to do with a name used twice, quietly removes that name
        # from the vocabulary rule.
        constants: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            values = node.value.values
            if not values or not all(
                isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tables[target.id] = [v.value for v in values]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.attr if isinstance(callee, ast.Attribute)
                else callee.id if isinstance(callee, ast.Name)
                else None
            )
            if name != "Check" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, module.name)
            elif isinstance(first, ast.Name) and first.id in constants:
                # A module-level constant in THIS module. One imported from
                # another falls through to `dynamic` on purpose: this sweep
                # reads one file at a time, so a name it cannot see written out
                # is a name it cannot police, and saying so is the point.
                found.setdefault(constants[first.id], module.name)
            elif isinstance(first, ast.Subscript) and isinstance(first.value, ast.Name):
                # A lookup into a module-level table of literal names. The names
                # ARE written out, just not at the call, so the sweep reads the
                # table instead of giving up — and if the table holds anything
                # that is not a plain string, this falls through to `dynamic`.
                table = tables.get(first.value.id)
                if table:
                    for literal in table:
                        found.setdefault(literal, module.name)
                else:
                    dynamic.append(f"{module.name}:{first.lineno}")
            else:
                # A name this cannot read is a name it cannot police, and it used
                # to skip them in silence: `f"x-mapsmith:{name}_is_close_to_..."`
                # in network.py was invisible to the whole sweep, and legal by
                # luck. Refusing outright is stronger than half-evaluating an
                # f-string, and it costs nothing: a check name is short.
                dynamic.append(f"{module.name}:{getattr(first, 'lineno', '?')}")

    return found, dynamic


def test_every_check_name_in_the_source_obeys_the_vocabulary():
    """Not only the names a fixture happens to trigger.

    The conformance sweep above sees the checks that actually fire on its
    fixtures, which on 2026-08-26 was 12 of the 16 extension names in the
    source: four live on conditional branches -- the axis-order probe in
    `run_sql`, the invented-class-code guard, the flat-length warning on 3D
    geometries, the repair-path input check -- and no fixture reaches them. All
    four turned out to be well formed, checked by hand. The next one might not
    be, and "checked by hand" is not a control.

    Read from the source with `ast` rather than by executing anything: a name on
    a branch nothing takes is exactly the case that matters here.
    """
    import sys
    from pathlib import Path

    # The rule is read from the vendored spec copy, not from a local
    # restatement of it: two copies of one mistake agree perfectly.
    sys.path.insert(0, str(Path(__file__).parent / "data"))
    from manifest_spec_validator import CORE_CHECK_NAMES, EXTENSION_CHECK_NAME

    found, dynamic = _check_names_in_source()

    assert not dynamic, (
        f"these check names are built at runtime rather than written out: {dynamic}. "
        "Write them literally — a name assembled from an f-string is invisible to "
        "this sweep, so the vocabulary rule stops applying to exactly the newest "
        "code, which is where it is most needed."
    )
    assert len(found) >= 25, f"only {len(found)} check names found in the source: {sorted(found)}"
    offenders = {
        check: where
        for check, where in found.items()
        if check not in CORE_CHECK_NAMES and not EXTENSION_CHECK_NAME.fullmatch(check)
    }
    assert not offenders, (
        f"these check names are neither a core name from section 3.6 of the spec nor an "
        f"extension `x-<producer>:<name>`: {offenders}. An unconstrained vocabulary makes "
        "two records incomparable, which is the point of having a format."
    )


def test_every_crs_decisions_key_in_the_source_obeys_the_spec():
    """The same rule as the sweep above, for the object beside it. D-077.

    **This is a MapSmith rule, stricter than the specification, and saying so is
    part of it.** Section 3.6 makes the check-name vocabulary a MUST with the
    syntax `x-<producer>:<name>`; that MUST is about `verification[].name` and
    nothing else. For FIELDS, section 3.5 only recommends -- "a producer prefix
    does this well", a SHOULD with no syntax -- and section 3.7 permits extra
    keys in `crs_decisions` under that rule. So a third-party producer reading
    only the specification would not emit our prefix, and this test is house
    policy rather than conformance until the specification says otherwise.

    What made the policy worth having is what a SHOULD with nothing reading it
    did. Derived from the source on 2026-09-06: **two sites had invented
    synonyms of keys section 3.7 already recommends and MapSmith already used
    everywhere else** -- `measurement_crs` in `points_along_lines`,
    `declared_output_crs` and `input_crs_before` in
    `transform_by_control_points`. A consumer asking those two records "what did
    you compute in?" read `analysis_crs`, found nothing, and had no way to know
    the answer sat under another name.

    Nothing could have caught it, because nothing compared the sites to each
    other -- each one is defensible alone. So the recommended keys are read from
    the vendored schema rather than restated here, the prefix predicate is
    shared with the runtime half in `conftest`, and the sites are read from the
    source rather than from the fixtures: a key on a branch no fixture reaches
    is exactly the case that produced this.
    """
    import ast
    from pathlib import Path

    import mapsmith

    fixed = _spec_crs_keys()
    root = Path(mapsmith.__file__).parent

    # Module-level string constants across the whole package, so a key written
    # as an IMPORTED name is still readable. The sibling sweep over check names
    # deliberately gives up on those, one file at a time; here it would give up
    # on exactly the right thing to do with a name used at three call sites --
    # `INPUTS_REPROJECTED` lives in `provenance` and is written in `linework`.
    # A name defined twice with different values is dropped rather than guessed.
    package_constants: dict[str, str | None] = {}
    for module in sorted(root.rglob("*.py")):
        for node in ast.parse(module.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                seen = package_constants.get(target.id, node.value.value)
                package_constants[target.id] = (
                    node.value.value if seen == node.value.value else None
                )

    found: dict[str, str] = {}
    dynamic: list[str] = []
    merged_from: set[str] = set()
    sites = 0
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        # Module-level string constants only, as in the sweep above: a
        # function-local name that happens to match one is a different name.
        constants: dict[str, str] = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        # ALL three tables map a name to a LIST, and every association is
        # resolved. The first version used dict comprehensions, so two functions
        # in one module that use the same local name collapsed into one and the
        # last won -- and `linework.py` binds a literal to `crs_decisions`
        # twice, in `snap_layer` and in `line_intersections`. An illegal key
        # injected into the first was invisible, which is to say this sweep was
        # blind exactly where it had just been used to find something. Two
        # reviewers found it independently on the day it was written; it was
        # only `subscripts`, written last, that already accumulated.
        from_call: dict[str, list[str]] = {}
        literals: dict[str, list[ast.Dict]] = {}
        subscripts: dict[str, list[ast.Constant]] = {}
        subscript_names: dict[str, list[ast.Name]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Dict):
                    literals.setdefault(node.target.id, []).append(node.value)
                continue
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Dict):
                        literals.setdefault(target.id, []).append(node.value)
                    elif isinstance(node.value, ast.Call):
                        callee = node.value.func
                        from_call.setdefault(target.id, []).append(
                            callee.attr if isinstance(callee, ast.Attribute)
                            else getattr(callee, "id", "?")
                        )
                elif isinstance(target, ast.Subscript) and isinstance(
                    target.value, ast.Name
                ):
                    # The key may be a literal or a constant's name. Anything
                    # else is recorded as unreadable rather than skipped: the
                    # first version required a literal and silently ignored
                    # `local[SOME_CONSTANT] = ...`, so three sites writing
                    # `crs_decisions[INPUTS_REPROJECTED]` were invisible to this
                    # sweep with no complaint. A guard that quietly declines to
                    # look is worse than one that says it cannot.
                    key = target.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        subscripts.setdefault(target.value.id, []).append(key)
                    elif isinstance(key, ast.Name):
                        subscript_names.setdefault(target.value.id, []).append(key)

        def resolve(
            value: ast.expr,
            where: str,
            depth: int = 0,
            # Bound here rather than closed over: this is redefined once per
            # module, and a closure over the loop's tables would read whichever
            # module happened to be last.
            *,
            subscripts: dict[str, list[ast.Constant]] = subscripts,
            subscript_names: dict[str, list[ast.Name]] = subscript_names,
            literals: dict[str, list[ast.Dict]] = literals,
            constants: dict[str, str] = constants,
            from_call: dict[str, list[str]] = from_call,
        ) -> None:
            """The keys this expression puts into `crs_decisions`, or a blind spot."""
            if depth > 3:  # pragma: no cover - a chain that deep is a defect anyway
                dynamic.append(f"{where}:{getattr(value, 'lineno', '?')}")
                return
            if isinstance(value, ast.IfExp):
                resolve(value.body, where, depth + 1)
                resolve(value.orelse, where, depth + 1)
                return
            if isinstance(value, ast.Call):
                # A dict this cannot read, built by a function. Named rather
                # than ignored: see the ratchet below.
                callee = value.func
                merged_from.add(
                    callee.attr if isinstance(callee, ast.Attribute)
                    else getattr(callee, "id", "?")
                )
                return
            if isinstance(value, ast.Name):
                seen = False
                for key in subscripts.get(value.id, []):
                    found.setdefault(key.value, f"{where}:{key.lineno}")
                    seen = True
                for name in subscript_names.get(value.id, []):
                    resolved = constants.get(name.id) or package_constants.get(name.id)
                    if resolved is None:
                        dynamic.append(f"{where}:{name.lineno}")
                    else:
                        found.setdefault(resolved, f"{where}:{name.lineno}")
                    seen = True
                for literal in literals.get(value.id, []):
                    resolve(literal, where, depth + 1)
                    seen = True
                for callee_name in from_call.get(value.id, []):
                    merged_from.add(callee_name)
                    seen = True
                if not seen:
                    dynamic.append(f"{where}:{value.lineno}")
                return
            if not isinstance(value, ast.Dict):
                dynamic.append(f"{where}:{getattr(value, 'lineno', '?')}")
                return
            for position, key in enumerate(value.keys):
                if key is None:
                    # `**something` inside the literal. Its keys are not here;
                    # name what built it, same as a merged call.
                    resolve(value.values[position], where, depth + 1)
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.setdefault(key.value, f"{where}:{key.lineno}")
                elif isinstance(key, ast.Name) and key.id in constants:
                    found.setdefault(constants[key.id], f"{where}:{key.lineno}")
                else:
                    dynamic.append(f"{where}:{getattr(key, 'lineno', '?')}")

        # Closed, not enumerative. The first version listed the three shapes it
        # knew -- assignment, keyword, `.update(dict)` -- and said nothing about
        # any other, so `record.crs_decisions["k"] = v`, `|= {...}`,
        # `.update(k=v)` and `.setdefault("k", v)` were all invisible rather
        # than red. That is the guard-that-cannot-fail turned on the guard's own
        # input: it could only ever report what it already understood. Now every
        # mention of the attribute is classified, and a shape with no branch
        # here fails the test instead of being skipped.
        parents: dict[int, ast.AST] = {
            id(child): node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        #: Methods that cannot introduce a key. Anything else on the attribute
        #: is reported rather than assumed harmless.
        reading = {"get", "items", "keys", "values", "copy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "crs_decisions":
                sites += 1
                resolve(node.value, module.name)
                continue
            if not (isinstance(node, ast.Attribute) and node.attr == "crs_decisions"):
                continue
            spot = f"{module.name}:{node.lineno}"
            parent = parents.get(id(node))
            # `record.crs_decisions = {...}` and `record.crs_decisions |= {...}`:
            # both hand the whole object over, so both resolve the right side.
            if (
                isinstance(parent, ast.Assign)
                and any(t is node for t in parent.targets)
            ) or (isinstance(parent, ast.AugAssign) and parent.target is node):
                sites += 1
                resolve(parent.value, module.name)
            elif isinstance(parent, ast.Attribute):
                call = parents.get(id(parent))
                if parent.attr in reading:
                    continue
                if parent.attr == "update" and isinstance(call, ast.Call):
                    sites += 1
                    for argument in call.args:
                        resolve(argument, module.name)
                    for keyword in call.keywords:
                        # `.update(analysis_crs=...)`: the key is the argument
                        # name, and `**other` has no name at all.
                        if keyword.arg is None:
                            resolve(keyword.value, module.name)
                        else:
                            found.setdefault(keyword.arg, spot)
                elif parent.attr == "setdefault" and isinstance(call, ast.Call):
                    sites += 1
                    first = call.args[0] if call.args else None
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.setdefault(first.value, spot)
                    else:
                        dynamic.append(spot)
                else:
                    dynamic.append(spot)
            elif isinstance(parent, ast.Subscript):
                grandparent = parents.get(id(parent))
                stored = isinstance(grandparent, ast.Assign) and any(
                    t is parent for t in grandparent.targets
                )
                if not stored:
                    continue  # a read: `record.crs_decisions["k"]` on the right
                sites += 1
                key = parent.slice
                resolved = (
                    key.value
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    else (constants.get(key.id) or package_constants.get(key.id))
                    if isinstance(key, ast.Name)
                    else None
                )
                if resolved is None:
                    dynamic.append(spot)
                else:
                    found.setdefault(resolved, spot)
            elif isinstance(getattr(node, "ctx", None), ast.Load):
                continue  # the attribute read as a whole value
            else:
                dynamic.append(spot)

    assert not dynamic, (
        f"these `crs_decisions` writes are not in a shape this can read: {dynamic}. "
        "Write the keys literally, or as a module-level constant -- and if the shape "
        "itself is new, teach this sweep about it rather than letting it pass."
    )
    # A smoke floor, and it says what it is: the control is `dynamic` being
    # empty, not this number. `len(found)` would not have moved if the sweep had
    # stopped reading sixty sites out of seventy, because it counts DISTINCT
    # keys -- so the floor is on the sites visited.
    assert sites >= 60, f"only {sites} write sites of `crs_decisions` seen in the source"

    # A key of ours has to be DECLARED, with the sentence saying why it is not a
    # synonym. The prefix rule above cannot ask that: `x-mapsmith:measurement_crs`
    # passes it, and `measurement_crs` is one of the two synonyms that started
    # all of this. Whether a name means what `source_crs` means is not a
    # mechanical question, so the ratchet does the only thing available -- it
    # makes whoever adds a key write the answer down.
    from mapsmith.provenance import CRS_EXTENSIONS

    undeclared = {
        key: where
        for key, where in found.items()
        if key.startswith("x-mapsmith:") and key not in CRS_EXTENSIONS
    }
    assert not undeclared, (
        f"these `crs_decisions` keys are ours and undeclared: {undeclared}. Add "
        "each to `provenance.CRS_EXTENSIONS` with the sentence that says why it is "
        "not a synonym of a key section 3.7 already recommends -- that sentence is "
        "the whole point, and a prefix does not supply it."
    )

    offenders = {
        key: where
        for key, where in found.items()
        if key not in fixed and not _EXTENSION_KEY.fullmatch(key)
    }
    assert not offenders, (
        f"these `crs_decisions` keys are neither recommended by section 3.7 of the "
        f"spec nor a MapSmith extension `x-mapsmith:<name>`: {offenders}. If the key "
        "means what a recommended one means, use the recommended one -- a synonym "
        "makes the record unreadable to a consumer holding the specification. If it "
        "is genuinely ours, prefix it, so that a reader holding the manifest and not "
        "the specification can tell which is which (D-077)."
    )

    # A ratchet, not an allowlist: an entry is a blind spot, not a thing to
    # check, and the three here are blind for reasons that were verified
    # rather than assumed. `alignment_decisions` builds the object for the
    # operations that bring a secondary input to one CRS, and its own keys
    # are pinned by `test_alignment_decisions_writes_only_conforming_keys`
    # below -- which exercises BOTH its branches, where the conformance
    # sweep only reaches the one where nothing moved. `grid.manifest_decisions` returns keys this sweep cannot
    # read and the conformance sweep over real records does. `redact_secrets`
    # cannot introduce a key at all: on a dict it rebuilds `{k: ...}` for the
    # keys it was given, so it re-emits whatever reached it and nothing else.
    # A third such function turns this red, so whoever adds it decides which
    # sweep covers it instead of discovering later that neither did.
    assert merged_from <= {
        "manifest_decisions",
        "redact_secrets",
        "alignment_decisions",
    }, (
        f"a dict is merged into `crs_decisions` from {sorted(merged_from)}, whose keys "
        "this sweep cannot read. Either write them out, or confirm the conformance "
        "sweep reaches every branch of it and add it here."
    )


def _spec_fixtures(tmp_path):
    """One real call per writing operation, for the conformance sweep.

    Real calls on real files: a hand-built record would validate the test's
    idea of a manifest rather than MapSmith's.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point, Polygon

    from mapsmith.engines import vector

    crs = "EPSG:32632"
    square = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    other = Polygon([(50, 50), (150, 50), (150, 150), (50, 150)])
    layer = tmp_path / "a.parquet"
    gpd.GeoDataFrame({"k": ["x"], "v": [1]}, geometry=[square], crs=crs).to_parquet(layer)
    second = tmp_path / "b.parquet"
    gpd.GeoDataFrame({"j": [2]}, geometry=[other], crs=crs).to_parquet(second)
    # One fixture on a GEOGRAPHIC datum, and NAD27 rather than WGS 84 on
    # purpose. Every other layer here is already projected, so until 2026-09-07
    # not one of the fifty-eight records this sweep collects had ever taken the
    # branch that reprojects and comes back -- which means `x-mapsmith:round_trip`
    # shipped, was declared, was documented, and was read by nothing. NAD27
    # crosses a datum in both directions (`estimate_utm_crs()` answers with a
    # WGS 84 zone whatever the input datum is), so the record here carries two
    # real transformations rather than two free relabellings.
    geographic = tmp_path / "nad27.parquet"
    gpd.GeoDataFrame(
        {"k": ["x"]},
        geometry=[Polygon([(-93.1, 34.5), (-93.0, 34.5), (-93.0, 34.6), (-93.1, 34.6)])],
        crs="EPSG:4267",
    ).to_parquet(geographic)
    points = tmp_path / "p.parquet"
    gpd.GeoDataFrame(
        {"n": [1, 2]}, geometry=[Point(10, 10), Point(90, 90)], crs=crs
    ).to_parquet(points)
    quad = tmp_path / "quad.parquet"
    gpd.GeoDataFrame(
        {"n": [1, 2, 3, 4], "v": [7.0, 7.0, 7.0, 7.0]},
        geometry=[Point(0, 0), Point(100, 0), Point(0, 100), Point(100, 100)],
        crs=crs,
    ).to_parquet(quad)

    # A small connected network and a strip of touching areas: everything the
    # network and statistics operations need, built once here rather than in
    # each lambda.
    streets = tmp_path / "streets.parquet"
    gpd.GeoDataFrame(
        {"id": [0, 1, 2]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
            LineString([(100, 0), (100, 100)]),
        ],
        crs=crs,
    ).to_parquet(streets)
    areas = tmp_path / "areas.parquet"
    gpd.GeoDataFrame(
        {"cases": [1.0, 1.0, 5.0, 1.0], "pop": [100.0, 200.0, 300.0, 400.0]},
        geometry=[
            Polygon([(i * 10, 0), (i * 10 + 10, 0), (i * 10 + 10, 10), (i * 10, 10)])
            for i in range(4)
        ],
        crs=crs,
    ).to_parquet(areas)
    crowd = tmp_path / "crowd.parquet"
    gpd.GeoDataFrame(
        {"weight": [1.0, 5.0, 2.0, 4.0]},
        geometry=[Point(x, 0) for x in (0, 10, 20, 30)],
        crs=crs,
    ).to_parquet(crowd)

    # For the linework operations: a line a few centimetres off its reference,
    # a pair that cross in a plus sign, and two control points describing a
    # quarter turn about the origin followed by a shift.
    nearly = tmp_path / "nearly.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0.03), (100, 0.03)])], crs=crs
    ).to_parquet(nearly)
    crossing = tmp_path / "crossing.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(50, -50), (50, 50)])], crs=crs
    ).to_parquet(crossing)
    baseline = tmp_path / "baseline.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0), (100, 0)])], crs=crs
    ).to_parquet(baseline)
    control = tmp_path / "control.parquet"
    gpd.GeoDataFrame(
        {"source_x": [0.0, 10.0], "source_y": [0.0, 0.0]},
        geometry=[Point(100, 200), Point(100, 210)],
        crs=crs,
    ).to_parquet(control)

    # A container with two layers, which nothing else here needs: extract_layer
    # has nothing to extract without one. Written with the GPKG driver because
    # single-dataset formats cannot hold two layers at all — which is the
    # refusal the operation exists to resolve.
    container = tmp_path / "container.gpkg"
    gpd.GeoDataFrame({"k": ["x"], "v": [1]}, geometry=[square], crs=crs).to_file(
        container, layer="parcels", driver="GPKG"
    )
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0), (100, 0)])], crs=crs
    ).to_file(container, layer="roads", driver="GPKG")

    def out(name: str) -> str:
        return str(tmp_path / name)

    from mapsmith.engines import linework, network, spatial_stats, whitebox_engine

    fixtures = {
        "network_shortest_path": lambda: network.network_shortest_path(
            str(streets), out("route.parquet"), 0, 0, 200, 0, tolerance=0.01
        ),
        "service_area": lambda: network.service_area(
            str(streets), out("reach.parquet"), 0, 0, budget=150.0, tolerance=0.01
        ),
        "hot_spots": lambda: spatial_stats.hot_spots(
            str(areas), out("gi.parquet"), value_field="cases", weights="contiguity"
        ),
        "smooth_rates": lambda: spatial_stats.smooth_rates(
            str(areas), out("eb.parquet"), count_field="cases", population_field="pop"
        ),
        "aggregate_to_threshold": lambda: spatial_stats.aggregate_to_threshold(
            str(areas), out("merged.parquet"), count_field="cases", minimum=2
        ),
        "thin_points": lambda: spatial_stats.thin_points(
            str(crowd), out("thin.parquet"), min_distance=15.0
        ),
        "snap_layer": lambda: linework.snap_layer(
            str(nearly), str(baseline), out("snapped.parquet"), tolerance=0.05
        ),
        "points_along_lines": lambda: linework.points_along_lines(
            str(baseline), out("chainage.parquet"), spacing=20.0
        ),
        "line_intersections": lambda: linework.line_intersections(
            str(baseline), str(crossing), out("nodes.parquet")
        ),
        "transform_by_control_points": lambda: linework.transform_by_control_points(
            str(baseline), str(control), out("placed.parquet"), target_crs=crs
        ),
        "contour_lines": lambda: whitebox_engine.contour_lines(
            _ramp(tmp_path), out("contours.parquet"), interval=3.0
        ),
        "least_cost_path": lambda: network.least_cost_path(
            _uniform_cost(tmp_path),
            _one_point(tmp_path, "lcp_start", 0.5, 9.5),
            _one_point(tmp_path, "lcp_end", 9.5, 9.5),
            out("cheapest.parquet"),
        ),
        "buffer_layer": lambda: vector.buffer(str(layer), 10.0, out("buf.parquet")),
        "clip_layer": lambda: vector.clip(str(layer), str(second), out("clip.parquet")),
        "overlay_layers": lambda: vector.overlay(
            str(layer), str(second), out("ov.parquet")
        ),
        "dissolve_layer": lambda: vector.dissolve(str(layer), out("dis.parquet"), by="k"),
        "nearest_join": lambda: vector.nearest_join(
            str(points), str(layer), out("near.parquet")
        ),
        "explode_layer": lambda: vector.explode(str(layer), out("exp.parquet")),
        "measure_area": lambda: vector.measure_area(str(layer), out("area.parquet")),
        "merge_layers": lambda: vector.merge(
            [str(layer), str(second)], out("merge.parquet")
        ),
        "simplify_layer": lambda: vector.simplify(str(layer), 1.0, out("simp.parquet")),
        # On the geographic fixture, so this sweep sees `x-mapsmith:round_trip`.
        "centroid_layer": lambda: vector.centroid(str(geographic), out("cent.parquet")),
        # `streets` and not `layer`: the single-polygon layer kept 1 of 1, so the
        # conformance sweep validated the manifest of a filter that filtered
        # nothing — output bytes identical to input, no `SUBSET` note, no
        # `feature_count_bounded` with anything to bound. Here two of three
        # survive, so the interesting branches are the ones checked.
        "select_features": lambda: vector.select_features(
            str(streets),
            out("selected.parquet"),
            by="field_in",
            field="id",
            values=[0, 1],
        ),
        "extract_layer": lambda: vector.extract_layer(
            str(container), "roads", out("extracted.parquet")
        ),
        "convert_format": lambda: vector.convert(str(layer), out("conv.gpkg")),
        "reproject_layer": lambda: vector.reproject(
            str(layer), "EPSG:4326", out("rep.parquet")
        ),
        "spatial_join": lambda: vector.spatial_join(
            str(points), str(layer), out("sj.parquet")
        ),
    }

    # --- the tier-A operations, whose fixtures are the traps' own -----------
    table = tmp_path / "table.csv"
    table.write_text("k,v\nx,10\n", encoding="utf-8")
    keyed = tmp_path / "keyed.parquet"
    gpd.GeoDataFrame({"k": ["x"]}, geometry=[square], crs=crs).to_parquet(keyed)
    weighted = tmp_path / "weighted.parquet"
    gpd.GeoDataFrame(
        {"value": [20.0, 1.0], "weight": [1000, 99000]},
        geometry=[square, other],
        crs=crs,
    ).to_parquet(weighted)
    dms = tmp_path / "dms.csv"
    dms.write_text("id,lat,lon\n1,41.89,12.49\n", encoding="utf-8")
    climbing = tmp_path / "climbing.parquet"
    gpd.GeoDataFrame(
        {"i": [1]}, geometry=[LineString([(0, 0, 0), (400, 0, 300)])], crs=crs
    ).to_parquet(climbing)

    fixtures.update({
        "join_table": lambda: vector.join_table(
            str(keyed), str(table), out("joined.parquet"), on="k"
        ),
        "measure_length": lambda: vector.measure_length(
            str(climbing), out("length.parquet"), method="3d"
        ),
        "aggregate_weighted": lambda: vector.aggregate_weighted(
            str(weighted), out("agg.parquet"),
            value_column="value", weight_column="weight",
        ),
        "parse_coordinates": lambda: vector.parse_coordinates(
            str(dms), out("parsed.parquet"),
            latitude_columns="lat", longitude_columns="lon",
        ),
        "point_on_surface": lambda: vector.point_on_surface(
            str(layer), out("pos.parquet")
        ),
        "hull_layer": lambda: vector.hull(str(layer), out("hull.parquet")),
        "validate_geometry": lambda: vector.validate_geometry(
            str(layer), out("valid.parquet")
        ),
        "count_in_polygons": lambda: vector.count_in_polygons(
            str(points), str(layer), out("counts.parquet")
        ),
        # Four points, because two collinear ones give two cells and no corner.
        "voronoi_polygons": lambda: vector.voronoi_polygons(
            str(quad), out("vor.parquet")
        ),
    })

    try:
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        from mapsmith.engines import raster
    except ImportError:
        return fixtures

    grid = tmp_path / "grid.tif"
    with rasterio.open(
        grid, "w", driver="GTiff", height=4, width=4, count=2, dtype="int16",
        crs=crs, transform=from_origin(0, 100, 25, 25), nodata=-9999,
    ) as ds:
        ds.write(np.arange(16, dtype="int16").reshape(4, 4), 1)
        ds.write(np.arange(16, 32, dtype="int16").reshape(4, 4), 2)

    # An AGREEING `.aux.xml` beside the raster, so that at least one record in
    # this sweep carries a non-empty `environment`. Until 2026-09-06 not one of
    # the fifty-eight did: the field that answers the first of the two silent
    # error classes -- the configuration nobody named -- had its shape covered
    # by nothing, and a guard written for its keys passed with the keys wrong.
    # Agreeing rather than contradicting on purpose: a disagreement is refused
    # outright by twelve operations (D-059), so it would test the refusal
    # instead of the record.
    (grid.parent / f"{grid.name}.aux.xml").write_text(
        "<PAMDataset><SRS>EPSG:32632</SRS></PAMDataset>", encoding="utf-8"
    )
    query = (
        f"SELECT * FROM read_parquet('{str(layer).replace(chr(92), '/')}')"
    )
    from mapsmith.engines import duckdb_engine

    fixtures["run_sql"] = lambda: duckdb_engine.run_sql(query, out("sql.parquet"))

    fixtures.update({
        "resample_raster": lambda: raster.resample(
            str(grid), out("res.tif"), 50, "nearest"
        ),
        "clip_raster": lambda: raster.clip_raster(
            str(grid), str(layer), out("clipr.tif")
        ),
        "reclassify_raster": lambda: raster.reclassify(
            str(grid), out("rc.tif"), ["0:8:1", "8:40:2"]
        ),
        "band_math": lambda: raster.band_math(str(grid), out("bm.tif"), "b2 - b1"),
        "zonal_statistics": lambda: raster.zonal_statistics(
            str(grid), str(layer), out("zs.parquet"), stats=["mean"]
        ),
        "reproject_raster": lambda: raster.reproject_raster(
            str(grid), out("repr.tif"), "EPSG:4326", "nearest"
        ),
        "extract_band": lambda: raster.extract_band(str(grid), out("band2.tif"), 2),
    })

    try:
        from mapsmith.engines import sampling, whitebox_engine
    except ImportError:
        return fixtures

    # A tilted plane with a single low corner: enough terrain for the
    # derivatives and the hydrology to have something to route.
    rows, cols = 24, 24
    yy, xx = np.mgrid[0:rows, 0:cols]
    surface = (100.0 + xx * 2.0 + yy * 1.0).astype("float32")
    dem = tmp_path / "dem.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs=crs, transform=from_origin(0, rows * 10.0, 10, 10), nodata=-9999.0,
    ) as ds:
        ds.write(surface, 1)
    pour = tmp_path / "pour.parquet"
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(5.0, 5.0)], crs=crs
    ).to_parquet(pour)
    mask = tmp_path / "mask.tif"
    features = np.zeros((rows, cols), dtype="float32")
    features[rows // 2, cols // 2] = 1.0
    with rasterio.open(
        mask, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs=crs, transform=from_origin(0, rows * 10.0, 10, 10), nodata=-9999.0,
    ) as ds:
        ds.write(features, 1)
    fixtures.update({
        "hillshade": lambda: whitebox_engine.hillshade(str(dem), out("hs.tif")),
        "slope": lambda: whitebox_engine.slope(str(dem), out("slope.tif")),
        "aspect": lambda: whitebox_engine.aspect(str(dem), out("aspect.tif")),
        "flow_accumulation": lambda: whitebox_engine.flow_accumulation(
            str(dem), out("facc.tif")
        ),
        "watershed": lambda: whitebox_engine.watershed(
            str(dem), str(pour), out("ws.tif")
        ),
        "focal_statistics": lambda: whitebox_engine.focal_statistics(
            str(dem), out("focal.tif"), statistic="mean", window=3
        ),
        "extract_streams": lambda: whitebox_engine.extract_streams(
            whitebox_engine.flow_accumulation(str(dem), out("facc_for_streams.tif"))
            and out("facc_for_streams.tif"),
            out("streams.tif"),
            threshold=5.0,
        ),
        "curvature": lambda: whitebox_engine.curvature(
            str(dem), out("curv.tif"), kind="profile"
        ),
        "flow_direction": lambda: whitebox_engine.flow_direction(
            str(dem), out("d8.tif"), method="d8"
        ),
        # A mask, not the DEM: euclidean_distance measures from the NON-ZERO
        # cells, and every cell of the DEM is non-zero, so it would be all zeros.
        "euclidean_distance": lambda: whitebox_engine.euclidean_distance(
            str(mask), out("dist.tif")
        ),
        "idw_interpolation": lambda: whitebox_engine.idw_interpolation(
            str(quad), out("idw.tif"), field_name="v", cell_size=10.0
        ),
        "viewshed": lambda: whitebox_engine.viewshed(
            str(dem), str(pour), out("seen.tif"), station_height=2.0
        ),
        "sample_raster_at_points": lambda: sampling.sample_raster_at_points(
            str(dem), str(points), out("sampled.parquet"), "bilinear"
        ),
        "elevation_profile": lambda: sampling.elevation_profile(
            str(dem), str(streets), out("profile.parquet"), spacing=25.0
        ),
    })
    return fixtures



def _ramp(tmp_path) -> str:
    """A planar DEM: z = column index, 10 m cells. Contours land on cell centres."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "ramp.tif"
    if not path.exists():
        values = np.tile(np.arange(12, dtype="float32"), (12, 1))
        with rasterio.open(
            path, "w", driver="GTiff", height=12, width=12, count=1,
            dtype="float32", crs="EPSG:32632",
            transform=from_origin(1000.0, 5000.0, 10.0, 10.0),
        ) as dst:
            dst.write(values, 1)
    return str(path)


def _uniform_cost(tmp_path) -> str:
    """Every cell costs 1, so the cheapest route is the straight one."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "cost.tif"
    if not path.exists():
        with rasterio.open(
            path, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs="EPSG:32632",
            transform=from_origin(0, 10, 1, 1),
        ) as dst:
            dst.write(np.ones((10, 10), dtype="float32"), 1)
    return str(path)


def _one_point(tmp_path, name: str, x: float, y: float) -> str:
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / f"{name}.parquet"
    if not path.exists():
        gpd.GeoDataFrame(
            {"id": [1]}, geometry=[Point(x, y)], crs="EPSG:32632"
        ).to_parquet(path)
    return str(path)


def test_a_crash_writes_a_manifest_the_published_validator_accepts(tmp_path):
    """Four call sites passed an empty precondition list, so an engine crash
    wrote `verification: []` — which the specification rejects, in its own
    words because a record with no checks is "a log entry wearing a manifest's
    clothes".

    Two of the four were operations added in 0.4.0: the module that arrived in
    this release did not inherit the pattern. The conformance sweep could not
    see it because it exercises only the paths that succeed.
    """
    import json
    from pathlib import Path

    from mapsmith.provenance import ProvenanceRecord

    out = tmp_path / "out.parquet"
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={"query": "SELECT 1"},
        inputs=[],
        engine={"name": "duckdb", "version": "1.5.5"},
    )
    with pytest.raises(RuntimeError), verify.audit_on_failure(record, str(out), []):
        raise RuntimeError("the engine blew up")

    manifest = json.loads(
        Path(f"{out}.provenance.json").read_text(encoding="utf-8")
    )
    assert manifest["verification"], "a crash wrote a record with no checks"
    assert manifest["verification"][0]["passed"] is False

    # BOTH implementations, via the helper this file already has. The first
    # version of this test used only the vendored validator — the lenient one of
    # the two — which is the thing `_spec_problems`'s own docstring forbids, and
    # a `pipeline: null` divergence between them was live at the time.
    assert _spec_problems(manifest) == [], _spec_problems(manifest)


def test_an_operation_that_verifies_nothing_raises_and_still_writes_the_record(tmp_path):
    """The success-path half of the same rule, and it used to be the other half.

    Until 2026-09-05 this test was called `..._raises_rather_than_writing` and
    asserted only the raise. That was deliberate — a record with no checks is
    "a log entry wearing a manifest's clothes", so the branch refused to write
    one — and it broke the requirement on the other side: the dataset is
    already on disk when `audited` runs, and §4 says a conforming producer
    emits a record for **every dataset it writes**. Raising left an orphan.

    Both hold when the absence is recorded as a failed check, which is what
    `audit_on_failure` does for a crash. So: the caller still gets its
    VerificationError, the orphan is gone, and the record that appears is
    conforming to both implementations — checked here, because a record
    written to satisfy the specification that the specification rejects would
    be worse than no record at all.
    """
    from mapsmith.provenance import ProvenanceRecord

    record = ProvenanceRecord(
        operation="pretend_operation",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    out = tmp_path / "out.parquet"
    out.write_bytes(b"")
    with pytest.raises(verify.VerificationError, match="no verification at all") as raised:
        verify.audited(
            record,
            str(out),
            operation="pretend_operation",
            checks_fn=list,
        )

    # The SENTENCE, not just the raise. `enforce` builds its message from the
    # failed check names, and every name that is not an input precondition fell
    # into the branch that says "output failed deterministic verification" —
    # false here, and false in the direction that matters: an agent reading
    # "output failed" retries or repairs the DATA, when the defect is in the
    # operation and a second run produces the same nothing.
    message = str(raised.value)
    assert "verified nothing about its output" in message
    assert "output failed" not in message

    written = out.with_name(out.name + ".provenance.json")
    assert written.exists(), "the dataset is on disk and its record is not"
    manifest = json.loads(written.read_text(encoding="utf-8"))
    assert [c["name"] for c in manifest["verification"]] == [
        "x-mapsmith:verification_present"
    ]
    assert manifest["verification"][0]["passed"] is False
    assert _spec_problems(manifest) == [], _spec_problems(manifest)


def _writers_by_shape() -> tuple[dict[str, str], dict[str, str]]:
    """Every dataset writer in the engines, split by how it reaches the manifest.

    Derived from the source rather than listed, because a list is worth exactly
    what somebody remembered to put in it: the point of this pair of tests is
    the writer nobody adds to a list.
    """
    import ast
    import pathlib

    import mapsmith.engines

    audited: dict[str, str] = {}
    inline: dict[str, str] = {}
    root = pathlib.Path(mapsmith.engines.__file__).parent
    for module in sorted(root.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.get_source_segment(text, node) or ""
            where = f"{module.name}:{node.name}"
            if "verify.audited(" in body:
                audited[where] = body
            elif ".write_for(" in body:
                # `.write_for(` and not the whole `.finish().write_for(output_path)`
                # line: keying on the literal meant a writer whose output
                # parameter happened to be named differently would be invisible
                # to both guards below — it would not fail, it would disappear,
                # which is the defect these guards exist to close, one level up.
                inline[where] = body
    return audited, inline


def test_no_writer_reaches_the_manifest_from_outside_the_engines():
    """The other half of the same worry: a writer in a module nobody scans.

    `_writers_by_shape` reads `engines/`, because that is where dataset writers
    live. That is a true statement about today and an assumption about tomorrow,
    so it is asserted rather than assumed: `write_for` may be called from the
    engines, from the module that defines it, and from the two helpers in
    `verify` that wrap the sequence. Anywhere else is a writer that neither
    guard can see.
    """
    import pathlib

    import mapsmith

    root = pathlib.Path(mapsmith.__file__).parent
    allowed = {"provenance.py", "verify.py"}
    stray = [
        str(module.relative_to(root))
        for module in sorted(root.rglob("*.py"))
        if module.parent.name != "engines"
        and module.name not in allowed
        and ".write_for(" in module.read_text(encoding="utf-8")
    ]
    assert not stray, (
        f"these modules write a manifest and live outside engines/, so neither "
        f"writer guard scans them: {stray}. Either move the writer, or widen "
        "`_writers_by_shape` — do not widen this allow-list without doing one."
    )


def test_every_writer_enforces_after_writing_the_manifest():
    """Manifest first, enforce second — and *enforce* is the half that can vanish.

    `mapsmith/CLAUDE.md` used to say that writers go through `verify.audited`
    "so the audit-trail-first invariant cannot be bypassed". Seventeen of the
    fifty-seven writers did not go through it, so the sentence was false on a
    public page, and one of the seventeen — `run_sql` — wrote its manifest and
    then never called `verify.enforce` at all. Its two checks are both
    non-critical, so nothing was wrong with the output; what was wrong is that a
    critical check added there later would have been recorded and not applied,
    which is the shape of defect this file exists to catch.
    """
    audited, inline = _writers_by_shape()
    assert audited, "no writer reaches the manifest through verify.audited"
    missing = sorted(
        where for where, body in inline.items() if "verify.enforce(" not in body
    )
    assert not missing, (
        "these writers build the manifest by hand and never enforce, so a "
        f"critical check would be recorded and ignored: {missing}"
    )


def test_the_number_of_hand_written_writers_only_goes_down():
    """A ratchet, not a pin.

    Open-coding the sequence is a migration in progress, not a second sanctioned
    shape (see the roadmap item). A new writer must use `verify.audited`, so
    this number may fall and must never rise — and when it falls, lower it here.
    """
    _, inline = _writers_by_shape()
    assert len(inline) <= 17, (
        f"{len(inline)} writers now build the manifest by hand, up from 17. A new "
        "writer goes through verify.audited: " + ", ".join(sorted(inline))
    )


def test_a_crash_manifest_that_cannot_be_written_says_so_on_the_exception(tmp_path):
    """The MUST that was protected by `suppress(Exception)`, which protects nothing.

    Section 3.1 of the manifest specification says a manifest MUST be written
    even when verification fails. `audit_on_failure` wrapped its write in
    `with suppress(Exception)`, so the audit trail this helper exists to save
    could itself vanish with nothing said anywhere — the contents of the crash
    record were made honest on 2026-09-02 while its existence stayed
    best-effort.

    Two behaviours are asserted, in the order that matters:

    1. Hashing the output is the only part of the write that reads a file the
       crashed engine may still hold, so when that raises the manifest is
       written WITHOUT `output` rather than not at all. A record with no
       `output` is valid and merely cannot be checked against bytes; a record
       that does not exist cannot be checked against anything.
    2. If even that fails, the loss is attached to the exception the caller
       already receives. Silence is the only outcome that is not allowed.
    """
    from mapsmith.provenance import ProvenanceRecord

    record = ProvenanceRecord(
        operation="pretend_operation",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    output = tmp_path / "held.parquet"
    output.write_bytes(b"some bytes")
    passed = verify.Check("input_crs_present", True, "EPSG:4326")

    def unreadable(_path):
        raise PermissionError("the engine still holds this file open")

    import mapsmith.provenance as prov

    original = prov.sha256_of
    prov.sha256_of = unreadable
    try:
        with (
            pytest.raises(RuntimeError, match="the engine died"),
            verify.audit_on_failure(record, str(output), [passed]),
        ):
            raise RuntimeError("the engine died")
    finally:
        prov.sha256_of = original

    written = tmp_path / "held.parquet.provenance.json"
    assert written.exists(), (
        "the output could not be hashed and the whole manifest was dropped — "
        "this is the MUST that suppress(Exception) used to hide"
    )
    manifest = json.loads(written.read_text(encoding="utf-8"))
    assert "output" not in manifest, "the digest was supposed to be the part dropped"
    names = [c["name"] for c in manifest["verification"]]
    assert "x-mapsmith:operation_completed" in names
    assert "input_crs_present" in names

    # 2. When nothing can be written, the caller is told on the exception it
    #    already has, rather than being left to discover the gap later.
    second = tmp_path / "gone" / "out.parquet"  # parent does not exist
    record2 = ProvenanceRecord(
        operation="pretend_operation",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    with (
        pytest.raises(RuntimeError) as raised,
        verify.audit_on_failure(record2, str(second), [passed]),
    ):
        raise RuntimeError("the engine died again")
    notes = " ".join(getattr(raised.value, "__notes__", []))
    assert "could not write the crash manifest" in notes, (
        "the manifest could not be written and nothing said so: "
        f"notes were {getattr(raised.value, '__notes__', [])!r}"
    )
    assert "no audit trail on disk" in notes


def test_a_crash_after_passing_preconditions_still_records_the_failure(tmp_path):
    """The `or` that made a crashed run look like a successful one.

    `audit_on_failure` recorded its failure check only when the precondition
    list was empty — `list(preconditions) or [...]`. Every operation that checks
    its inputs first passes a non-empty list, which is about 59 paths counting
    the ones that reach it through `verify.audited`, and on those an engine
    crash wrote a manifest containing nothing but the checks that had already
    passed. Conforming, and unreadable in the one way that matters: it reads
    like a success.

    The point of the test is the combination — a passing precondition AND a
    crash — because each half alone was already fine.
    """
    from mapsmith.provenance import ProvenanceRecord

    record = ProvenanceRecord(
        operation="pretend_operation",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    output = tmp_path / "out.parquet"
    output.write_bytes(b"")
    passed = verify.Check("input_crs_present", True, "EPSG:4326")

    with (
        pytest.raises(RuntimeError, match="the engine died"),
        verify.audit_on_failure(record, str(output), [passed]),
    ):
        raise RuntimeError("the engine died")

    manifest = json.loads(
        (tmp_path / "out.parquet.provenance.json").read_text(encoding="utf-8")
    )
    names = [c["name"] for c in manifest["verification"]]
    assert "input_crs_present" in names, "the precondition was dropped"
    assert "x-mapsmith:operation_completed" in names, (
        "the run crashed and the manifest says every check passed — this is the "
        "record reading like a success"
    )
    failed = [c for c in manifest["verification"] if not c["passed"]]
    assert len(failed) == 1 and failed[0]["name"] == "x-mapsmith:operation_completed"
    # And it does not claim the passing checks say anything about the result.
    assert "BEFORE that point" in failed[0]["detail"]


def test_alignment_decisions_writes_only_conforming_keys():
    """The keys of the one helper both sweeps have to take on trust.

    `alignment_decisions` is a function, so the AST sweep records it as a blind
    spot rather than reading through it, and the conformance sweep only reaches
    its quiet branch — the fixtures hand every operation inputs that already
    share a CRS, so nothing is ever reprojected in them. Between the two, the
    branch that emits the most keys is seen by neither.

    So it is pinned here, on both branches, against the same predicate the
    other two use. Not a list of expected keys: whatever it emits has to be a
    key section 3.7 recommends or a MapSmith extension, and a key added
    tomorrow is judged by the rule rather than by whether somebody remembered
    to add it to an assertion.
    """
    from mapsmith.provenance import alignment_decisions

    fixed = _spec_crs_keys()
    cases = {
        "nothing moved": alignment_decisions("EPSG:32632", "already aligned"),
        "one moved": alignment_decisions(
            "EPSG:32632", "the mask was aligned", [("mask_path", "EPSG:4267")]
        ),
        "several moved": alignment_decisions(
            "EPSG:32632",
            "two layers were aligned",
            [("input_2", "EPSG:4326"), ("input_3", "EPSG:4267")],
        ),
    }
    for label, decisions in cases.items():
        offenders = [
            key
            for key in decisions
            if key not in fixed and not _EXTENSION_KEY.fullmatch(key)
        ]
        assert not offenders, f"{label}: {offenders}"

    # And the substance, because conforming keys with nothing in them would
    # pass the paragraph above. One input moved: the transformation goes in the
    # specification's own key. Several: there is no single transformation, so
    # each entry carries its own and the top-level key is absent rather than
    # holding one of them and implying it covers both.
    one = cases["one moved"]
    assert isinstance(one["transformation"]["is_ballpark"], bool)
    assert one[INPUTS_REPROJECTED] == [{"argument": "mask_path", "from": "EPSG:4267"}]

    several = cases["several moved"]
    assert "transformation" not in several
    assert [entry["argument"] for entry in several[INPUTS_REPROJECTED]] == [
        "input_2",
        "input_3",
    ]
    assert all(
        isinstance(entry["transformation"]["is_ballpark"], bool)
        for entry in several[INPUTS_REPROJECTED]
    )

    assert INPUTS_REPROJECTED not in cases["nothing moved"]


def test_every_reprojected_input_names_a_real_argument():
    """A wrong argument name in the record is worse than no name at all.

    `INPUTS_REPROJECTED` exists so a reader knows WHICH input was moved. On
    2026-09-06 `watershed` recorded `points_path` and the operation's argument
    is `pour_points_path` -- the local variable's name, written down instead of
    the caller's. A reader looking for `points_path` in their own call finds
    nothing and concludes the record is about some other run.

    **Read from the source, and the first version of this test was not.** It
    ran every operation and inspected the records, which sounds stronger and is
    weaker: the fixtures hand every operation inputs that already share a CRS,
    so no reprojection branch is ever taken and there was nothing to inspect.
    Restoring the wrong name left it green. That is the guard-that-cannot-fail
    again, written by the person who spent the day removing them -- and it is
    why the sabotage runs before the test is believed.
    """
    import ast
    from pathlib import Path

    import mapsmith
    from mapsmith import catalog
    from mapsmith.plans.registry import BINDINGS

    # The names a caller can actually pass, per engine function. The catalogue
    # is keyed by operation name and the engine function may be called something
    # else (`buffer` implements `buffer_layer`), so the binding is what joins
    # them -- the same join `test_path_containment` makes.
    parameters = {
        entry["name"]: {
            parameter["name"] if isinstance(parameter, dict) else str(parameter)
            for parameter in entry.get("parameters", [])
        }
        for entry in catalog.OPERATIONS
    }
    # The binding carries the caller-facing names too, and they are the
    # authority: `input_args` is what the plan validator checks and what an
    # agent writes. Joined to the engine function by the operation the function
    # NAMES ITSELF -- `ProvenanceRecord(operation="clip_layer")` -- because the
    # binding's `loader` is a wrapper, not the engine function, and matching on
    # `__name__` joined nothing at all.
    allowed: dict[str, set[str]] = {}
    for operation, binding in BINDINGS.items():
        allowed[operation] = (
            set(parameters.get(operation, ()))
            | set(getattr(binding, "input_args", ()) or ())
            | set(getattr(binding, "list_input_args", ()) or ())
        )

    root = Path(mapsmith.__file__).parent
    named: list[tuple[str, str, str]] = []
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            declares = [
                keyword.value.value
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
                and getattr(call.func, "id", None) == "ProvenanceRecord"
                for keyword in call.keywords
                if keyword.arg == "operation"
                and isinstance(keyword.value, ast.Constant)
            ]
            uses_helper = any(
                isinstance(call, ast.Call)
                and (
                    call.func.attr if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", None)
                )
                == "alignment_decisions"
                for call in ast.walk(function)
            )
            if not uses_helper:
                continue
            # Every two-tuple with a string literal first, anywhere in a function
            # that calls the helper. Reading only the third positional argument
            # found nothing: at the real call sites `moved` is a conditional
            # expression, a comprehension or a variable built earlier, and a
            # sweep that only understands a list literal understands none of the
            # twelve.
            for node in ast.walk(function):
                if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
                    continue
                first = node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    for operation in declares:
                        named.append((module.name, operation, first.value))

    assert len(named) >= 8, (
        f"only {len(named)} argument names read from the source: the sweep is not "
        "finding the call sites any more"
    )
    wrong = [
        f"{module}:{operation} names {argument!r}"
        for module, operation, argument in named
        if argument not in allowed.get(operation, set())
    ]
    assert not wrong, (
        f"these name an input that is not an argument of their operation: {wrong}. "
        "The point of the key is that a reader can find the argument in their own "
        "call; a local variable's name sends them looking for something that is "
        "not there."
    )


def test_every_declared_extension_is_actually_written_somewhere():
    """The other half of the registry, asked where it can be answered.

    The AST sweep cannot ask it: three of the six keys are written inside
    `alignment_decisions` and `manifest_decisions`, which it records as blind
    spots by design rather than reading through. Comparing the registry against
    what that sweep sees therefore called three live keys stale -- the check was
    wrong, not the code.

    So the question is asked of the source as a whole: a declared key that no
    line writes is a name nobody emits, and a registry of those is a list that
    grows and never shrinks. The reason each key exists is in the registry; this
    only asks that the key still does.
    """
    # The registry's OWN declaration is cut out of the text first, and without
    # that this test cannot fail: declaring a key is what puts it in the source,
    # so every entry would find itself. Fourth guard-that-cannot-fail written
    # today, and the sabotage found it in seconds (D-079).
    import ast
    from pathlib import Path

    import mapsmith
    from mapsmith.provenance import CRS_EXTENSIONS

    root = Path(mapsmith.__file__).parent
    home = root / "provenance.py"
    lines = home.read_text(encoding="utf-8").splitlines(keepends=True)
    for node in ast.parse("".join(lines)).body:
        targets = getattr(node, "targets", []) or [getattr(node, "target", None)]
        if any(getattr(t, "id", None) == "CRS_EXTENSIONS" for t in targets if t):
            del lines[node.lineno - 1 : node.end_lineno]
            break
    else:  # pragma: no cover - the registry moved and this test is now blind
        raise AssertionError("CRS_EXTENSIONS is no longer a module-level assignment")

    source = "".join(lines) + chr(10).join(
        module.read_text(encoding="utf-8")
        for module in sorted(root.rglob("*.py"))
        if module != home
    )
    unwritten = [
        key
        for key, name in (
            (key, key.removeprefix("x-mapsmith:")) for key in CRS_EXTENSIONS
        )
        if key not in source and name not in source
    ]
    assert not unwritten, (
        f"these are declared in CRS_EXTENSIONS and written by no line: {unwritten}. "
        "Remove them — a registry of names nobody emits is a list that only grows, "
        "and the next reader believes it."
    )
    assert len(CRS_EXTENSIONS) >= 5, (
        f"only {len(CRS_EXTENSIONS)} extensions declared, which cannot be right"
    )
    thin = [key for key, why in CRS_EXTENSIONS.items() if len(why) < 80]
    assert not thin, (
        f"these are declared with no real reason: {thin}. The sentence is the point "
        "of the registry: it is what a prefix cannot supply."
    )


def test_the_published_vocabulary_is_what_the_source_says_today():
    """The page of names, and the two derivations of them that must agree.

    A vocabulary a consumer has to read the engines to learn is half a
    vocabulary, so `docs/manifest-vocabulary.md` lists it — generated, because
    a hand-written list of sixty names is worth what whoever last remembered to
    update it was worth.

    The assertion that earns its place is the second one. There are now two
    readings of the same set: the generator's, which lists, and the sweep
    above, which polices. Two copies of one rule is how a rule quietly becomes
    two rules — and this project has watched exactly that happen to a manifest
    validator and its schema, which had drifted on every recommended field. So
    the two are compared, and a name either derivation finds alone is a
    failure.
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "benchmarks"))
    import manifest_vocabulary as vocabulary

    published = (root / "docs" / "manifest-vocabulary.md").read_text(encoding="utf-8")
    assert published == vocabulary.page(), (
        "docs/manifest-vocabulary.md is not what the source says now. Regenerate "
        "with `python benchmarks/manifest_vocabulary.py --write` and read the diff: "
        "a name that appeared or vanished is a change to what a consumer can "
        "branch on."
    )

    # The two derivations, compared. `_check_names_in_source` is the policing
    # one — it refuses names it cannot read rather than skipping them, so it is
    # the stricter of the two and the generator must not see more than it does.
    listed = set(vocabulary.check_names())
    policed, _ = _check_names_in_source()
    policed = set(policed)
    assert listed == policed, (
        "the generator and the vocabulary guard disagree about which check names "
        f"exist. Only the generator: {sorted(listed - policed)}. Only the guard: "
        f"{sorted(policed - listed)}. One rule, two readings, and they have "
        "started to drift."
    )
