"""The front door has to lead everywhere, and say only true things.

The benchmark results were public for a day and nobody could find them: no
link from the README. Writing something and publishing something are not the
same act, and the difference is invisible to every other test in this suite.
These checks are mechanical on purpose — taste cannot be automated, but
reachability, dead links and stale numbers can.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SITE_TEMPLATE = ROOT / "site" / "index.template.html"
SITE_BUILD = ROOT / "site" / "build.py"
LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")

# Root pages a visitor is expected to find from the front door. CLAUDE.md is
# deliberately not here: it is a contributor guide for AI assistants, which
# clients read by convention, not a page we ask a human to navigate to.
ROOT_PAGES_NOT_LINKED_ON_PURPOSE = {"README.md", "CLAUDE.md"}


def _showcase_pages() -> list[Path]:
    """Every markdown page the showcase is made of, wherever it lives."""
    pages = [README, *sorted(ROOT.glob("*.md")), *sorted((ROOT / "docs").glob("*.md"))]
    pages += [ROOT / "examples" / "README.md", ROOT / "benchmarks" / "gabench-ab" / "README.md"]
    seen: dict[Path, None] = {}
    for page in pages:
        if page.exists():
            seen.setdefault(page.resolve(), None)
    return list(seen)


def _links(markdown: Path) -> set[str]:
    return {m.group(1) for m in LINK.finditer(markdown.read_text(encoding="utf-8"))}


def _local_links(markdown: Path) -> set[str]:
    return {t for t in _links(markdown) if not t.startswith(("http://", "https://", "mailto:"))}


def _reachable_from_readme() -> set[Path]:
    """Files linked by the README, plus files linked by those (two clicks)."""
    seen: set[Path] = set()
    frontier = [README]
    for _ in range(2):
        nxt = []
        for page in frontier:
            for target in _local_links(page):
                resolved = (page.parent / target).resolve()
                if not resolved.exists():
                    continue
                # linking a directory reaches its README: that is the page
                # GitHub renders for it
                if resolved.is_dir() and (resolved / "README.md").exists():
                    resolved = (resolved / "README.md").resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    if resolved.suffix == ".md":
                        nxt.append(resolved)
        frontier = nxt
    return seen


@pytest.mark.parametrize(
    "page", _showcase_pages(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_every_local_link_resolves(page):
    """A broken relative link is a 404 for every visitor."""
    missing = [t for t in _local_links(page) if not (page.parent / t).exists()]
    assert not missing, f"{page.name} links to files that do not exist: {missing}"


def test_every_docs_page_is_reachable_from_the_readme():
    """Publishing a page nobody can navigate to is not publishing it."""
    reachable = _reachable_from_readme()
    orphans = [
        p.name for p in sorted((ROOT / "docs").glob("*.md")) if p.resolve() not in reachable
    ]
    assert not orphans, (
        f"docs pages unreachable from the README within two clicks: {orphans}. "
        "Link them, or they do not exist as far as a visitor is concerned."
    )


def test_every_readme_anchor_points_at_a_heading():
    """In-page links are how the first screen reaches the proof further down;
    renaming a heading silently turns them into scroll-to-nowhere."""
    text = README.read_text(encoding="utf-8")
    slugs = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
        slugs.add(re.sub(r"\s+", "-", slug))
    anchors = set(re.findall(r"\]\(#([^)\s]+)\)", text))
    assert anchors <= slugs, f"README anchors with no matching heading: {sorted(anchors - slugs)}"


def test_every_root_page_is_reachable_from_the_readme():
    """SECURITY.md sat unlinked for two releases: GitHub renders a tab for it,
    so nobody noticed the README never pointed at the one page that documents
    what MapSmith does *not* protect."""
    reachable = _reachable_from_readme()
    orphans = [
        p.name
        for p in sorted(ROOT.glob("*.md"))
        if p.name not in ROOT_PAGES_NOT_LINKED_ON_PURPOSE and p.resolve() not in reachable
    ]
    assert not orphans, (
        f"root pages unreachable from the README within two clicks: {orphans}. "
        "Link them, or accept that visitors will never read them."
    )


def test_the_notebook_gallery_is_reachable():
    reachable = _reachable_from_readme()
    notebooks = sorted((ROOT / "examples").glob("*.ipynb"))
    assert notebooks, "the gallery lost its notebooks"
    # the gallery README counts as the entry point for the notebooks
    gallery = (ROOT / "examples" / "README.md").resolve()
    assert gallery in reachable, "the notebook gallery is not linked from the README"
    linked = _local_links(ROOT / "examples" / "README.md")
    unlinked = [n.name for n in notebooks if n.name not in linked]
    assert not unlinked, f"notebooks missing from the gallery index: {unlinked}"


def test_the_readme_tool_table_matches_the_registered_tools():
    """A tool table that drifts turns the front page into documentation of a
    product that no longer exists."""
    from mapsmith import server

    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", README.read_text(encoding="utf-8"), re.MULTILINE))
    assert registered == documented, (
        f"missing from the README: {sorted(registered - documented)}; "
        f"documented but gone: {sorted(documented - registered)}"
    )


def test_the_readme_tool_count_matches_the_registered_tools():
    """"16 goal-level tools" is a number, and numbers rot. The claim appears
    twice (the pitch and the limitations), so both have to move together."""
    from mapsmith import server

    registered = len(server.mcp._tool_manager.list_tools())
    text = README.read_text(encoding="utf-8")
    counted = {int(n) for n in re.findall(r"\b(\d+)\s+(?:goal-level\s+)?tools\b", text)}
    wrong = {n for n in counted if n != registered}
    assert not wrong, (
        f"the README claims {sorted(wrong)} tools but {registered} are registered"
    )


def test_the_roadmap_does_not_list_a_shipped_tool_as_future_work():
    """A roadmap that promises what already runs makes the rest look sloppy."""
    from mapsmith import server

    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    todo = re.findall(r"^- \[ \] (.+)$", README.read_text(encoding="utf-8"), re.MULTILINE)
    shipped_but_promised = {
        name for name in registered for item in todo if re.search(rf"\b{name}\b", item)
    }
    assert not shipped_but_promised, (
        f"the roadmap lists shipped tools as future work: {sorted(shipped_but_promised)}. "
        "Tick the box, or say precisely which part is still missing."
    )


def test_the_changelog_covers_the_released_version():
    """A release with no entry means the visitor cannot tell what changed —
    and the version in pyproject is what PyPI will publish, so they must agree."""
    from mapsmith import __version__

    # regex, not tomllib: a substring check over the raw text stays true however
    # the dependency is expressed — extra, marker or version pin
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert declared and declared.group(1) == __version__, (
        f"pyproject says {declared.group(1) if declared else '?'}, "
        f"the package says {__version__}"
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(__version__)}\]", changelog, re.MULTILINE), (
        f"CHANGELOG.md has no section for the current version {__version__}"
    )


def test_server_json_declares_the_version_being_released():
    """The registry entry carries the version in three places — the server, the
    PyPI package and the OCI tag — and a release bumps them by hand. One left
    behind publishes a listing that points at the previous image."""
    import json

    from mapsmith import __version__

    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert data["version"] == __version__, (
        f"server.json says {data['version']}, the package says {__version__}"
    )
    for package in data["packages"]:
        if package["registryType"] == "pypi":
            assert package["version"] == __version__, (
                f"server.json pypi package pinned at {package['version']}"
            )
        if package["registryType"] == "oci":
            assert package["identifier"].endswith(f":{__version__}"), (
                f"server.json OCI tag is {package['identifier']}, not :{__version__}"
            )


def test_the_citation_file_matches_the_released_version():
    """Zenodo mints a DOI from this file on every GitHub release, and whatever it
    says at that instant is archived permanently. A stale version here does not
    fail a build or bother a user: it produces a citation that names the wrong
    software, in a record that cannot be edited away."""
    import datetime as dt

    from mapsmith import __version__

    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")

    declared = re.search(r'^version:\s*"([^"]+)"', citation, re.MULTILINE)
    assert declared, "CITATION.cff has no version"
    assert declared.group(1) == __version__, (
        f"CITATION.cff says {declared.group(1)}, the package says {__version__}. "
        f"A release archives this file as it stands."
    )

    released = re.search(r'^date-released:\s*"(\d{4}-\d{2}-\d{2})"', citation, re.MULTILINE)
    assert released, "CITATION.cff has no date-released"
    assert dt.date.fromisoformat(released.group(1)) <= dt.datetime.now(tz=dt.UTC).date(), (
        f"CITATION.cff claims a release on {released.group(1)}, which has not happened. "
        f"The date is written by hand before tagging, and a slipped tag leaves it false."
    )

    assert "mapsmith-ai/MapSmith" in citation, "repository-code does not point at this repository"


def test_the_archive_metadata_uses_a_licence_identifier_zenodo_resolves():
    """SPDX and Zenodo disagree about case, and nothing warns you.

    CFF requires SPDX identifiers, written `AGPL-3.0-or-later`. Zenodo's licence
    vocabulary is keyed lowercase and returns 404 on anything else, so citation
    metadata that is correct by its own standard names a licence the archive
    cannot resolve, and the release is archived without it.

    That is why this file exists rather than letting CITATION.cff speak for
    itself. Its sibling repositories learned the same thing twice: a list of
    licences where one string was required, then a capital letter.
    """
    import json

    metadata = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    licence = metadata.get("license")

    assert isinstance(licence, str), (
        f"license must be a single string, got {type(licence).__name__}: {licence!r}"
    )
    assert licence == licence.lower(), (
        f"Zenodo's licence identifiers are lowercase and it 404s on anything "
        f"else; {licence!r} would not resolve"
    )


def test_the_archive_metadata_does_not_contradict_the_rest_of_the_release():
    """Zenodo ignores CITATION.cff entirely when .zenodo.json is present, so the
    two carry the same facts twice and nothing at release time notices them
    drifting. This is the cost of the fix above, paid here."""
    import json

    from mapsmith import __version__

    metadata = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")

    assert metadata["version"] == __version__, (
        f".zenodo.json archives {metadata['version']} while the package is "
        f"{__version__}. Zenodo reads this file from inside the tag, so a "
        f"release cannot correct it afterwards."
    )
    title = re.search(r'^title:\s*"(.+)"', citation, re.MULTILINE)
    assert title and metadata["title"] == title.group(1), (
        f".zenodo.json titles the record {metadata['title']!r}; CITATION.cff "
        f"says {title.group(1) if title else None!r}"
    )
    for creator in (c["name"] for c in metadata["creators"]):
        assert creator in citation, (
            f".zenodo.json credits {creator!r}, absent from CITATION.cff"
        )


def test_the_registry_ownership_proof_is_in_every_place_it_is_read_from():
    """The MCP Registry proves ownership by matching one string in three
    artifacts: the README marker it reads from the repository, the Dockerfile
    label it reads from the image, and server.json itself. Any of them missing
    or drifting fails publication — at release time, which is the most
    expensive moment to find out."""
    import json

    name = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["name"]
    readme = README.read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {name} -->" in readme, (
        f"the README has no '<!-- mcp-name: {name} -->' marker"
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f'LABEL io.modelcontextprotocol.server.name="{name}"' in dockerfile, (
        f"the Dockerfile does not label the image with {name}"
    )


def test_the_funding_manifest_is_valid_and_findable_by_a_human():
    """funding.json is a content category of its own, and the failure mode is
    the familiar one: a crawler finds it at the root, a visitor never does. It
    is also a page of claims — keep it parseable and keep it linked."""
    import json

    manifest = ROOT / "funding.json"
    assert manifest.exists(), "funding.json is gone"
    data = json.loads(manifest.read_text(encoding="utf-8"))  # invalid JSON = invisible
    assert data.get("projects"), "funding.json declares no project"
    assert "funding.json" in README.read_text(encoding="utf-8"), (
        "funding.json is mentioned nowhere a visitor reads: link it from the README"
    )


def test_the_word_gis_survives_where_a_search_can_see_it():
    """MapSmith was absent from every curated list of GIS MCP servers while its
    package description, its registry entry and its first line all said
    "geoprocessing" and never "GIS" — which is the word people search for. A
    rewrite that drops it again should fail here, not in six months of silence."""
    import json

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    description = re.search(r'^description\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert description and "GIS" in description.group(1), (
        "the PyPI description does not contain the word GIS"
    )
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["description"]
    assert "GIS" in registry, "the MCP Registry description does not contain the word GIS"
    first_screen = README.read_text(encoding="utf-8").split("## Quickstart")[0]
    assert "GIS" in first_screen, "the README says what it is without ever saying GIS"


# --------------------------------------------------------------------------
# mapsmith.dev. The site is the other front door, and until 2026-08-25 no test
# looked at it: the workflow checked that no placeholder survived the build and
# nothing checked what the page said. A generated page drifts exactly like a
# README, and worse, because the repository still reads correctly while it does.
# --------------------------------------------------------------------------


def test_the_site_names_only_tools_that_exist():
    """A name on the published page outlives the thing it describes.

    Checked against tools AND catalogue operations, because since D-037 an
    operation is reachable without a tool of its own: `point_on_surface` has no
    tool and is not a ghost. The guard still bites on a name that was removed or
    never existed, which is what it is for.

    Every `<table class="tools">` is checked, not the first one. It used to read
    only the first, and the moment a second table was added above the tool table
    the guard started checking the wrong one — reporting a real operation as a
    ghost, which is how this was noticed.
    """
    from mapsmith import catalog, server

    known = {t.name for t in server.mcp._tool_manager.list_tools()}
    known |= {op["name"] for op in catalog.OPERATIONS}
    tables = re.findall(
        r'<table class="tools".*?</table>', SITE_TEMPLATE.read_text(encoding="utf-8"), re.DOTALL
    )
    assert tables, "the site template lost its tool table"
    named = {n for table in tables for n in re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", table)}
    ghosts = sorted(named - known)
    assert not ghosts, (
        f"mapsmith.dev names things that do not exist: {ghosts}. "
        "The page is generated, so nothing else will ever notice."
    )

def test_every_number_on_the_site_comes_from_a_placeholder_the_build_fills():
    """Both directions. A `{{NEW_COUNT}}` nobody fills breaks the build (which
    is fine, it is loud); a placeholder the build still computes and the page
    no longer shows is a claim that quietly left the shop window."""
    template = SITE_TEMPLATE.read_text(encoding="utf-8")
    build = SITE_BUILD.read_text(encoding="utf-8")
    in_page = set(re.findall(r"\{\{[A-Z_]+\}\}", template))
    assert in_page, "the site template lost its placeholders"
    filled = {p for p in re.findall(r'"(\{\{[A-Z_]+\}\})"', build)}
    assert not in_page - filled, f"placeholders the build does not fill: {sorted(in_page - filled)}"
    assert not filled - in_page, (
        f"the build computes values the page no longer shows: {sorted(filled - in_page)}"
    )


def test_the_twin_project_is_linked_from_both_of_our_front_doors():
    """The mirror image of the defect a reader found on 2026-08-24: argleton.org
    was linked from its own repository's homepage field, which nobody sees, and
    from nowhere in its README. Here the risk is the same one facing outward —
    the strongest evidence MapSmith has is a suite that grades it and lives in
    another organisation, so both front doors have to point at it, and the
    README has to do it on the first screen rather than in a roadmap entry
    four hundred lines down."""
    first_screen = README.read_text(encoding="utf-8").split("## Quickstart")[0]
    assert "argleton.org" in first_screen, (
        "the README's first screen does not link the correctness suite that grades MapSmith"
    )
    assert "argleton.org" in SITE_TEMPLATE.read_text(encoding="utf-8"), (
        "mapsmith.dev states the silent-error problem and never points at the instrument "
        "that measures it"
    )
    assert "argleton" in (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8").lower(), (
        "benchmarks.md is where the hand-off to Argleton is declared; it no longer names it"
    )


# Words that promise instead of stating. Each one has a concrete replacement:
# a number, a mechanism, or a link to a measurement.
_MARKETING = (
    "revolutionary", "seamless", "blazing", "cutting-edge", "game-chang", "effortless",
    "state-of-the-art", "unleash", "supercharge", "next-generation", "world-class",
    "turbocharg", "magical",
)


@pytest.mark.parametrize(
    "page", _showcase_pages(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
)
def test_the_showcase_states_instead_of_selling(page):
    """The audience is GIS engineers: an adjective where a number belongs reads
    as a claim nobody measured."""
    text = page.read_text(encoding="utf-8").lower()
    found = sorted({w for w in _MARKETING if w in text})
    assert not found, f"{page.name} sells instead of stating: {found}"


def test_every_screenshot_is_actually_shown():
    """An image nobody links to is either a stale UI we forgot to delete or a
    screenshot we forgot to publish. Both are worth one line of test."""
    images = sorted((ROOT / "docs" / "images").glob("*"))
    if not images:
        return
    referenced = {
        (page.parent / target).resolve()
        for page in _showcase_pages()
        for target in _local_links(page)
    }
    unused = [i.name for i in images if i.resolve() not in referenced]
    assert not unused, f"images in docs/images nobody displays: {unused}"


def _notebook_output_text(notebook: Path) -> str:
    import json

    cells = json.loads(notebook.read_text(encoding="utf-8"))["cells"]
    chunks = []
    for cell in cells:
        for output in cell.get("outputs", []):
            chunks.append("".join(output.get("text", [])))
            chunks.append(json.dumps(output.get("data", {})))
            if output.get("output_type") == "error":
                chunks.append("\n".join(output.get("traceback", [])))
    return "\n".join(chunks)


@pytest.mark.parametrize(
    "notebook", sorted((ROOT / "examples").glob("*.ipynb")), ids=lambda p: p.name
)
def test_the_gallery_shows_the_current_release(notebook):
    """The notebooks are committed *with their outputs*: that is the point (a
    visitor reads results without running anything), and it is also how a
    manifest from the previous release stays on display forever.

    This half only catches a *wrong* version. It cannot catch a missing one —
    see the test below, which is the half that was absent while two notebooks
    displayed manifests from before 0.3.0.
    """
    from mapsmith import __version__

    text = _notebook_output_text(notebook)
    shown = set(re.findall(r'"mapsmith_version":\s*"([^"]+)"', text))
    assert shown <= {__version__}, (
        f"{notebook.name} displays manifests from {sorted(shown)} but this is "
        f"{__version__} — re-run the notebook instead of editing its output"
    )


def _manifests_shown_in(notebook: Path) -> list[dict]:
    """Every complete provenance manifest printed in a notebook's output.

    Parsed rather than pattern-matched, which is the point: a manifest printed
    through a slice — `json.dumps(manifest, indent=2)[:1500]`, which is what
    notebook 01 did — does not parse, so it is not counted, so the "at least
    one" assertion below fails instead of quietly having nothing to check.
    """
    text = _notebook_output_text(notebook)
    decoder = json.JSONDecoder()
    found = []
    for start in re.finditer(r"^\{", text, re.MULTILINE):
        try:
            candidate, _ = decoder.raw_decode(text[start.start() :])
        except ValueError:
            continue
        if isinstance(candidate, dict) and {"operation", "engine"} <= candidate.keys():
            found.append(candidate)
    return found


#: Which notebooks are expected to display a full provenance manifest. Declared
#: rather than counted, because summing over the gallery was itself vacuous: the
#: first version of this test asserted `sum(...) > 0`, which notebook 01 satisfies
#: on its own, so 02 and 03 could have gone back to showing pre-0.3.0 records with
#: the suite still green. That is the defect this test exists to close, one level
#: up. A notebook that starts or stops showing one fails here until somebody says
#: so on purpose — the safe direction.
NOTEBOOKS_SHOWING_A_MANIFEST = {"01_verified_geoprocessing.ipynb"}


def test_the_gallery_displays_a_conforming_manifest():
    """The vacuous half, and the one that was missing.

    `shown <= {__version__}` is true of the empty set. Two notebooks displayed
    manifests written before 0.3.0 — no `spec_version`, which the specification
    *requires*, and no `producer`, which it recommends — and `"path":
    "data\\\\wells.gpkg"` with the Windows separator that issue #30 fixed and the
    README says is fixed. The guard read a field those manifests do not have,
    found nothing, compared nothing to `{__version__}`, and passed. It had been
    passing for two releases.

    So the claim is stated positively and checked positively: the gallery shows
    a manifest *where it is meant to*, it parses, and it conforms — against both
    the validator and the schema, for the reason the front-page guard gives.
    """
    import importlib.util

    from mapsmith import __version__

    notebooks = sorted((ROOT / "examples").glob("*.ipynb"))
    shown = {n.name: _manifests_shown_in(n) for n in notebooks}
    displaying = {name for name, found in shown.items() if found}
    assert displaying == NOTEBOOKS_SHOWING_A_MANIFEST, (
        f"the gallery displays manifests in {sorted(displaying)}, and this test "
        f"expects {sorted(NOTEBOOKS_SHOWING_A_MANIFEST)}. A notebook that stopped "
        "showing one may be printing it through a slice, which no longer parses; "
        "a notebook that started showing one needs adding here, so that it is "
        "checked rather than merely tolerated."
    )

    spec = importlib.util.spec_from_file_location(
        "spec_validator", ROOT / "tests" / "data" / "manifest_spec_validator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "tests" / "data" / "manifest-v1.schema.json").read_text(encoding="utf-8")
    )
    checker = jsonschema.Draft202012Validator(schema)

    for name, manifests in shown.items():
        for manifest in manifests:
            problems = module.problems(manifest) + [
                error.message for error in checker.iter_errors(manifest)
            ]
            assert problems == [], (
                f"{name} displays a manifest that is not a conforming record: "
                f"{problems}. Re-run the notebook; do not edit its output."
            )
            assert manifest.get("producer", {}).get("version") == __version__, (
                f"{name} displays a manifest produced by "
                f"{manifest.get('producer', {}).get('version')!r} and this is "
                f"{__version__} — re-run it."
            )
            for recorded in manifest.get("inputs", []):
                assert "\\" not in recorded.get("path", ""), (
                    f"{name} displays a manifest with a Windows separator in "
                    f"{recorded['path']!r}. Paths in a manifest are POSIX on "
                    "every platform (#30), and the gallery is where a reader "
                    "checks whether that is true."
                )


@pytest.mark.parametrize(
    "notebook", sorted((ROOT / "examples").glob("*.ipynb")), ids=lambda p: p.name
)
def test_the_gallery_notebooks_ran_clean(notebook):
    """An unexecuted cell or a traceback in the gallery reads as "this does not
    work", whatever the surrounding prose says."""
    import json

    cells = json.loads(notebook.read_text(encoding="utf-8"))["cells"]
    code = [c for c in cells if c["cell_type"] == "code" and "".join(c["source"]).strip()]
    silent = [i for i, c in enumerate(code) if not c.get("outputs")]
    errors = [
        i
        for i, c in enumerate(code)
        if any(o.get("output_type") == "error" for o in c.get("outputs", []))
    ]
    assert not errors, f"{notebook.name} ships a traceback in cells {errors}"
    assert not silent, (
        f"{notebook.name} has code cells with no saved output ({silent}): the gallery "
        "is meant to be readable without running it"
    )


def test_no_stale_version_strings_in_the_docs():
    """The provenance example in the README carries a version; a stale one
    tells visitors they are reading about an old release."""
    from mapsmith import __version__

    quoted = set(re.findall(r'"mapsmith_version":\s*"([^"]+)"', README.read_text(encoding="utf-8")))
    assert quoted <= {__version__}, (
        f"README shows mapsmith_version {sorted(quoted)} but this is {__version__}"
    )


def test_the_readme_says_which_release_it_describes():
    """The version in PROSE, which the JSON check above does not see.

    Both live within thirty lines of each other, both name a release, and the
    0.4.0 release commit moved one and not the other: the manifest example said
    0.4.0 while the sentence above it still said "this page describes 0.3.0" --
    on a page documenting seventy-four operations, a stack switch and a manifest
    field that 0.3.0 does not have. The reader most exposed is the one arriving
    on release day, which is precisely when nobody re-reads the paragraph.

    The sentence also promises to name the difference when `main` runs ahead of
    the published artifact, so a mismatch is allowed -- as long as the page says
    which is which rather than simply naming the wrong one.
    """
    from mapsmith import __version__

    text = README.read_text(encoding="utf-8")
    match = re.search(r"This page describes \*\*([0-9][^*]*)\*\*", text)
    assert match, (
        "the sentence saying which release this page describes has been "
        "reworded or removed; update this test with it rather than dropping it"
    )
    stated = match.group(1)
    if stated != __version__:
        nearby = text[match.start() : match.start() + 400]
        ahead = "ahead" in nearby
        assert ahead, (
            f"the README says it describes {stated} and this checkout is "
            f"{__version__}. That is allowed only while the paragraph also says "
            "main is ahead and names the difference -- it does not."
        )


def test_declared_dependencies_are_not_advertised_as_future_work():
    """"More to come: X" for an X that already ships reads as either sloppy or
    dishonest, and both cost the same."""
    text = README.read_text(encoding="utf-8")
    # read as text, not via tomllib: a substring check stays true however the
    # dependency is expressed — extra, marker or version pin
    all_deps = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # substrings, so the check survives renaming "WhiteboxTools" to the name of
    # the library we actually depend on ("Whitebox Workflows")
    shipped = {"Whitebox": "whitebox", "DuckDB": "duckdb", "exactextract": "exactextract"}
    coming = re.search(r"more to come:([^)]*)\)", text)
    if not coming:
        return
    wrong = [name for name, pkg in shipped.items() if pkg in all_deps and name in coming.group(1)]
    assert not wrong, f"listed as future work but already a dependency: {wrong}"


# A URL written bare in markdown is rendered as a clickable link by GitHub. For
# an illustrative address that is not meant to resolve, that produces a link a
# reader follows and finds nothing — which reads as a broken page rather than as
# an example. Fenced or inline code is not linked, so that is where they belong.
BARE_URL = re.compile(r"(?<![(`\[<])https?://[^\s)\]`<>\"]+")
ILLUSTRATIVE_HOSTS = ("evil.tld", "example.com", "example.org", "attacker", "internal.")


@pytest.mark.parametrize("page", _showcase_pages(), ids=lambda p: p.name)
def test_an_illustrative_url_is_never_rendered_as_a_link(page: Path):
    """`https://evil.tld/x.gpkg` explains an attack; it is not somewhere to go."""
    text = page.read_text(encoding="utf-8")
    # Code fences are already safe, and stripping them keeps the check honest
    # rather than making authors escape things twice.
    outside_code = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    offenders = [
        m.group(0)
        for m in BARE_URL.finditer(outside_code)
        if any(host in m.group(0) for host in ILLUSTRATIVE_HOSTS)
    ]
    assert not offenders, (
        f"{page.name}: illustrative URLs rendered as clickable links — wrap them in "
        f"backticks so a reader does not follow one and find nothing: {offenders}"
    )


# Vendor names we do not write in public, at all: no note, no table row, no
# comparison. The reason is not politeness — it is that a public comparison
# invites a reply we have no standing to have, and a single sentence about a
# vendor is enough to frame this project as competing with one rather than
# measuring anything. The GDAL driver short names below are the one exception:
# they are identifiers GDAL itself emits, and the deny-list test that keeps new
# drivers from arriving unreviewed cannot spell them any other way.
VENDOR_SILENCE = re.compile(r"esri|arcgis|arcpy|arcmap", re.IGNORECASE)
# Identifiers that belong to somebody else's API, not mentions of a vendor: two
# GDAL driver names and one keyword argument of whitebox-workflows. A rule about
# what the project SAYS cannot forbid the spelling of a function parameter it has
# to pass -- but the list stays short and explicit, so a new entry is a decision
# rather than a hole.
DRIVER_IDENTIFIERS = (
    "ESRI Shapefile", "ESRIJSON", "esri_pntr",
    # The value of a manifest's `engine.name`, and of the module that produces
    # it. A record that could not name the engine that made the numbers would
    # be useless, and naming it is the opposite of a comparison — it is the
    # admission that a different engine ran. Same principle as the driver names
    # above: an identifier of somebody else's thing, not a claim about it.
    "\"ArcGIS Pro\"", "ArcGISPro.exe",
)


def _tracked_text_files() -> list[Path]:
    """Every tracked file a stranger can read, source included."""
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    keep = {".md", ".py", ".html", ".yml", ".yaml", ".toml", ".json", ".cff", ".txt", ".ipynb"}
    return [
        ROOT / name
        for name in out
        if name and (ROOT / name).suffix in keep and (ROOT / name).exists()
    ]


#: What a comparison looks like when it is written down. A percentage, a score,
#: a ratio, or a word that ranks. A plain count is none of these: "2198 tools
#: are installed, 1274 run on your licence" describes the reader's own machine
#: and is the fact that explains why a fallback exists.
COMPARATIVE = re.compile(
    r"\d+(\.\d+)?\s*%"
    r"|\b0\.\d+\b"
    r"|\b\d+\s*/\s*\d+\b"
    # Words only where they RANK, which in English means they carry a "than".
    # The bare adjectives were tried and are too common in ordinary prose:
    # "behind an extension" and "worse than a match" are not benchmarks, and a
    # guard that cries on those gets switched off — which leaves less
    # protection than a narrower rule that nobody disables.
    r"|\b(faster|slower|better|worse|more accurate|less accurate)\s+than\b"
    r"|\b(beats|outperforms)\b",
    re.IGNORECASE,
)
#: How far from the name a figure still reads as being about it. Two lines of
#: prose: far enough to catch "ArcGIS Pro. … 35% of tasks", short enough that an
#: unrelated number three paragraphs down does not trip it.
COMPARISON_WINDOW = 160


def test_no_vendor_is_named_beside_a_figure():
    """The name is allowed; the name next to a number is not (D-057).

    This used to forbid the name outright, and D-044 was right to while Esri was
    only something we measured. D-056 made it a MODE of the product — a stack
    the caller can choose — and a product with an Esri mode names it in the
    README, in the opening handshake, and in the message that says the licence
    is missing. Silence and the mode cannot both exist.

    So the line moved to where it actually protects something: **integrating is
    not comparing.** Saying "MapSmith calls what you have installed" describes
    this project. Putting a score beside that name is a benchmark, and D-057
    keeps benchmarks unpublished.

    What is deliberately NOT relaxed: Argleton's own guard still forbids the
    name outright, because Argleton is the surface a comparison would live on.
    """
    offenders = {}
    for page in _tracked_text_files():
        if page.resolve() == Path(__file__).resolve():
            continue  # this file names them in order to constrain them
        text = page.read_text(encoding="utf-8", errors="replace")
        for identifier in DRIVER_IDENTIFIERS:
            text = text.replace(identifier, "")
        hits = []
        for match in VENDOR_SILENCE.finditer(text):
            start = max(0, match.start() - COMPARISON_WINDOW)
            window = text[start : match.end() + COMPARISON_WINDOW]
            figure = COMPARATIVE.search(window)
            if figure:
                line = text[: match.start()].count("\n") + 1
                hits.append(f"line {line}: {match.group(0)!r} near {figure.group(0)!r}")
        if hits:
            offenders[str(page.relative_to(ROOT)).replace("\\", "/")] = hits
    assert not offenders, (
        "a vendor is named beside a figure, which is a comparison however it is "
        f"phrased, and D-057 keeps those unpublished: {offenders}. Say the thing "
        "without the number, or move the number somewhere the name is not."
    )


NUMBER_WORDS_TO_INT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def test_the_pages_count_the_operations_that_actually_refuse_an_ambiguous_raster():
    """A count of callers, stated in prose on two surfaces, counted from src/.

    This page promised the refusal for a whole release while the function had
    **no caller in src/** — a test called it directly and went green. The fix
    gave it nine callers, and the prose then said "an operation that computes
    from the georeferencing refuses", which is a different overstatement: the
    terrain and sampling operations do not call it, so sixteen of the
    twenty-five raster operations were covered by a sentence and not by code.

    Now the pages name the number, which means the number can go stale the next
    time one is wired — so it is counted here instead of remembered. Prose in
    words, not digits, on purpose: a phrase is what a reader believes, and a
    phrase is what this reads.
    """
    callers = set()
    for source in (ROOT / "src" / "mapsmith").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        callers.update(
            re.findall(
                r"""refuse_ambiguous_georeferencing\(\s*[^,]+,\s*["']([a-z_]+)["']""",
                text,
            )
        )
    assert callers, (
        "nothing in src/ calls refuse_ambiguous_georeferencing. That was the "
        "defect: the README promised the refusal, a decision record declared it, "
        "and the only caller was a test."
    )

    stated = []
    for page in (README, SITE_TEMPLATE):
        text = page.read_text(encoding="utf-8")
        for word in re.findall(
            r"\b([a-z]+) operations that read a raster's grid directly", text
        ):
            stated.append((page.name, word))
    assert stated, (
        "neither the README nor the site page states how many operations refuse "
        "an ambiguously georeferenced raster. Say the number — the version "
        "without one said 'an operation that computes', which is every raster "
        "operation and was false for sixteen of them."
    )
    for name, word in stated:
        assert NUMBER_WORDS_TO_INT.get(word) == len(callers), (
            f"{name} says {word!r} operations refuse an ambiguous raster and "
            f"{len(callers)} call the refusal: {sorted(callers)}"
        )


def test_the_roadmap_does_not_list_a_shipped_operation_as_future_work():
    """The tool-name version of this check stopped being enough.

    Operations can now ship without a tool of their own, so a capability can be
    live and still sit unticked on the roadmap — which is how "stream network
    extraction" stayed listed as future work on the day `extract_streams`
    shipped. Names are matched by stem so the prose form of a capability
    ("stream network extraction") is caught alongside the identifier.

    Parentheticals are stripped before matching, because that is where an item
    names a shipped operation as *context* for something new rather than as the
    promise: "per-zone embedding vectors (multiband zonal statistics)" promises
    the embeddings, not `zonal_statistics`. Matching the main clause keeps the
    check sharp without an allow-list that would grow until it meant nothing."""
    from mapsmith import catalog

    available = [
        entry["name"] for entry in catalog.OPERATIONS if entry.get("status") == "available"
    ]
    todo = re.findall(r"^- \[ \] (.+)$", README.read_text(encoding="utf-8"), re.MULTILINE)
    offenders = {}
    for name in available:
        stems = [token.rstrip("s") for token in name.split("_") if len(token.rstrip("s")) > 3]
        if not stems:
            continue
        for item in todo:
            promise = re.sub(r"\([^)]*\)", "", item)
            if all(re.search(rf"\b{stem}", promise, re.IGNORECASE) for stem in stems):
                offenders[name] = item
    assert not offenders, (
        f"the roadmap lists shipped operations as future work: {offenders}. "
        "Tick the box, or say precisely which part is still missing."
    )


def test_no_public_page_states_a_python_floor_that_disagrees_with_the_package():
    """`pyproject.toml` is the floor; a page that names a different one is a lie
    a newcomer discovers as a failed install.

    CONTRIBUTING said 3.10 for a day after the floor moved to 3.12 (D-038), and
    the only reader who would ever notice is the one it costs the most."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'requires-python\s*=\s*">=\s*(\d+\.\d+)"', pyproject)
    assert declared, "pyproject.toml no longer declares requires-python in a readable form"
    floor = declared.group(1)
    stated = re.compile(r"Python\s*(?:>=|≥|>)\s*(\d+\.\d+)")
    offenders = {}
    for page in _showcase_pages():
        for found in stated.findall(page.read_text(encoding="utf-8")):
            if found != floor:
                offenders[str(page.relative_to(ROOT)).replace("\\", "/")] = found
    assert not offenders, (
        f"pyproject requires Python >={floor} but these pages say otherwise: {offenders}"
    )

def test_every_test_fixture_is_tracked_by_git():
    """A file the tests read must be a file CI has.

    `tests/data/` is ignored wholesale with per-filename exceptions, which is
    the right default for datasets and the wrong shape for vendored
    dependencies: the exception is a filename, so the SECOND vendored file is
    ignored by default. That has now happened twice -- the spec validator, then
    the schema beside it on 2026-08-26 -- and both times the symptom was a suite
    that passed locally and a CI that could not find the file. The `.gitignore`
    comment recording the first one did not prevent the second, because prose
    does not run."""
    import subprocess

    tracked = set(
        subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "tests/data"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    )
    on_disk = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests" / "data").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    missing = sorted(on_disk - tracked)
    assert not missing, (
        f"these files are read by the tests and not tracked by git: {missing}. "
        "CI checks out the repository, not this machine: add them, or the suite "
        "passes here and fails there."
    )

NUMBER_WORDS = {
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
    21: "twenty-one", 22: "twenty-two", 23: "twenty-three", 24: "twenty-four",
    25: "twenty-five", 26: "twenty-six",
}


def test_the_changelog_block_for_this_version_counts_what_is_actually_here():
    """The counts in `[Unreleased]` age exactly like the README's, and nothing watched them.

    On 2026-08-28 that block said "18 → 27" against 28 registered tools (and 18→28
    is ten, not nine), "41 operations (39 available, 2 planned)" against 51/49/2,
    and `spec_version 1.0.0-draft.2` against the draft.3 the code emits. Three
    stale numbers in the file a packager reads to decide what a release contains.
    """
    from mapsmith import __version__, catalog, server

    # The block for the version in the tree, not simply the first one. Cutting
    # the 0.3.0 release moved these counts out of `[Unreleased]` and into a
    # dated section, and a test anchored on "the first block" would have gone on
    # passing over an empty one — which is worse than failing.
    changelog = README.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
    blocks = changelog.split("## [")
    block = next(
        (b for b in blocks if b.startswith(f"{__version__}]")),
        next((b for b in blocks if b.startswith("Unreleased]")), ""),
    )
    assert block.strip(), (
        f"the changelog has no section for {__version__} and no [Unreleased] one, "
        "so nothing here is checking the counts a packager reads"
    )

    tools = len(asyncio.run(server.mcp.list_tools()))
    total = len(catalog.OPERATIONS)
    available = sum(1 for op in catalog.OPERATIONS if op["status"] == "available")
    planned = total - available

    wrong = []
    # Anchored on the sentence that states the shipped shape. Loose patterns match
    # prose about the 800-operation scale projection, which is a different number
    # about a different catalogue.
    for pattern, actual, what in (
        (r"18 → (\d+)\.\*\*", tools, "tools"),
        (r"catalogue is\s+at (\d+) operations", total, "catalogue operations"),
        (r"operations \((\d+) available", available, "available operations"),
        (r"available, (\d+) planned\)", planned, "planned operations"),
    ):
        for found in re.findall(pattern, block):
            if int(found) != actual:
                wrong.append(f"{what}: CHANGELOG says {found}, there are {actual}")
    assert not wrong, (
        "the [Unreleased] block describes a different product from the one in this "
        f"tree: {wrong}"
    )

    from mapsmith import provenance

    emitted = provenance.SPEC_VERSION
    for found in re.findall(r"`(1\.0\.0-draft\.\d+)`", block):
        assert found == emitted, (
            f"the CHANGELOG announces spec_version {found} and the writers emit "
            f"{emitted}"
        )


def test_no_page_cites_an_argleton_run_other_than_the_vendored_one():
    """The citation guards the numbers; this guards the pointer beside them.

    A run folder and a `spec_commit` are what a reader clicks to check a number,
    and they aged separately from the number itself: on 2026-08-28 the table in
    `docs/benchmarks.md` carried the right figures under a link to the previous
    day's run, which happened to have the same ones. A stale link under a correct
    number is worse than a stale number, because it looks verified.

    The same applies to the family count written as a word. "Nineteen-family run"
    survived a family being added because no test reads English numerals.
    """
    citation = json.loads((ROOT / "docs" / "argleton-run.json").read_text(encoding="utf-8"))
    run, commit, families = citation["run"], citation["spec_commit"], citation["families"]

    pages = [README, SITE_TEMPLATE, *(ROOT / "docs").glob("*.md")]
    wrong = []
    for page in pages:
        prose = page.read_text(encoding="utf-8")
        for stale in re.findall(r"results/(20\d\d-\d\d-\d\d[a-z0-9-]*)", prose):
            if stale != run:
                wrong.append(f"{page.name}: links results/{stale}, citation says {run}")
        if f"`{commit}`" not in prose and f"results/{run}" in prose:
            wrong.append(f"{page.name}: links the run but does not quote spec_commit {commit}")
        for n, word in NUMBER_WORDS.items():
            if f"{word}-family" in prose.lower() and n != families:
                wrong.append(
                    f"{page.name}: says '{word}-family' where the citation has {families}"
                )
    assert not wrong, (
        "these pages point at an Argleton run that is not the published one, so a "
        f"reader checking a number lands on the wrong folder: {wrong}"
    )


def test_every_argleton_number_quoted_here_matches_the_vendored_citation():
    """Argleton's numbers are the ones this repo cannot count for itself.

    Tool, test and catalogue counts come from this source tree, so the build
    computes them and they cannot drift. Argleton is a separate repository in a
    separate organisation — on purpose — so its numbers arrive as prose, and
    prose ages: the trap count was hand-typed and went stale three times in four
    days (eight families, then eighteen, then twenty traps).

    `docs/argleton-run.json` is the single vendored citation, written by the
    publish script when a run is published. Everything quoted here must agree
    with it, and the site template must not hand-type the count at all.
    """
    import json

    citation = json.loads((ROOT / "docs" / "argleton-run.json").read_text(encoding="utf-8"))
    traps = citation["traps_run"]
    spelled = {
        18: "eighteen", 19: "nineteen", 20: "twenty", 21: "twenty-one",
        22: "twenty-two", 23: "twenty-three", 24: "twenty-four", 25: "twenty-five",
        26: "twenty-six", 27: "twenty-seven", 28: "twenty-eight", 29: "twenty-nine",
        30: "thirty",
    }

    template = (ROOT / "site" / "index.template.html").read_text(encoding="utf-8")
    assert "{{TRAP_COUNT}}" in template, (
        "the site template no longer reads the trap count from the citation"
    )
    for wrong in (v for k, v in spelled.items() if k != traps):
        assert f"{wrong} traps" not in template.lower(), (
            f"the template hand-types {wrong!r} traps; the published run has {traps}"
        )

    # Markdown is prose and cannot hold a placeholder, so it is checked instead:
    # any trap count it states must be the published one. README and docs/, which
    # are the pages a reader arrives at.
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    checked = 0
    for page in pages:
        text = page.read_text(encoding="utf-8").lower()
        for number, word in spelled.items():
            for phrase in (f"{word} traps", f"over {number} traps", f"on {number} traps"):
                if phrase in text:
                    checked += 1
                    assert number == traps, (
                        f"{page.name} says {phrase!r}; the published run "
                        f"({citation['run']}) has {traps} traps"
                    )
    assert checked, (
        "no page states a trap count any more — if the claim moved, move this check with it "
        "rather than leaving it passing over nothing"
    )


def test_the_readme_catalog_counts_are_the_real_ones():
    """Two numbers in the discovery section, both hand-typed, both already wrong.

    The sentence read "41 today, and 28 of them have no tool of their own" on
    27/08/2026 with 51 entries and 26 without a tool -- and the 28 was not a
    stale count of anything, it was the count of exposed TOOLS from a paragraph
    further up, reused for a different claim. The site build computes the first
    number from the catalog; the README states it in prose, so it needs this.
    """
    import re

    from mapsmith import catalog

    text = README.read_text(encoding="utf-8")
    match = re.search(
        r"operation MapSmith can perform — (\d+) today, and (\d+) of them have no "
        r"tool of their own",
        text,
    )
    assert match, (
        "the sentence carrying the catalog counts has been reworded; update this "
        "test with it rather than deleting it"
    )
    stated_total, stated_toolless = int(match.group(1)), int(match.group(2))
    assert stated_total == len(catalog.OPERATIONS)
    assert stated_toolless == sum(
        1 for entry in catalog.OPERATIONS if entry.get("tool") is None
    )


def test_the_retrieval_numbers_agree_between_the_readme_and_the_site():
    """The same measurement is quoted on two surfaces, in prose, in two formats.

    Exactly the shape that has rotted twice in this repository. The build
    generates the tool and catalogue counts, but it cannot generate these —
    running the measurement would make every site build load an embedding model
    and embed 850 documents — so the guard is that the two surfaces cannot
    disagree, while `test_retrieval_at_scale` says whether what they agree on is
    still true.

    The two pages do not carry the SAME amount of detail on purpose: the README
    is where the ablation tables live, the site compresses them. So this checks
    the claims that appear on both, not that one is a copy of the other.
    """
    readme = README.read_text(encoding="utf-8")
    site = SITE_TEMPLATE.read_text(encoding="utf-8")

    # The product's own numbers, and the ones the clarification path rests on.
    # If one surface is edited and the other is not, this is what catches it.
    # This list has been rewritten twice in two days, and both times because a
    # published number was wrong rather than because a surface drifted. 70% came
    # from twenty queries we wrote ourselves; 51% replaced it from the independent
    # set; 48% is the same measurement over the larger set, and it stopped being
    # the headline when the answer became a delivered SET rather than a ranking.
    # Whichever numbers are current have to be on both surfaces, which is the
    # whole job of this test.
    shared = [
        ("118", "independent requests the retrieval numbers come from"),
        # Was "60%" until 2026-08-29, and it had been stale for three catalogue
        # sizes: it passed only because that string happened to appear in two
        # unrelated sentences on the two pages. A shared-number check that
        # matches by coincidence is worse than one that is missing.
        ("58%", "our ranking, found@3, once arity is declared"),
        ("69%", "a model choosing from the delivered candidates"),
        ("70%", "where the two model labellers agree with each other"),
        ("4.4", "plausible families per request, why family cannot filter"),
        # Replaced the 800-operation projection on 2026-08-29: the wall it
        # projected arrived at sixty-one, so the pages now carry what happened
        # rather than what was expected to.
        ("34", "candidates before arity was declared — the set that broke it"),
        ("45%", "delivered while the set was too large to hand over"),
        ("0.90", "top-3 agreement when an answer exists"),
        ("0.18", "top-3 agreement when it does not"),
        ("9 of 11", "unanswerable queries the clarification catches"),
        ("centroid_layer", "the defect the discovery contract found"),
    ]
    missing = [
        f"{value} ({what})"
        for value, what in shared
        if value not in readme or value not in site
    ]
    assert not missing, (
        "these claims are not on both surfaces any more, so one of them has been "
        f"edited and the other has not: {missing}"
    )

    # And a number that appears on both must appear with the same value. The
    # ablation table lives in the README; where the site repeats one of its rows,
    # the row has to match.
    import re

    rows = re.findall(r"^[|] (\d+) [|] (\d+)% [|] (\d+)% [|]$", readme, re.MULTILINE)
    for size, lexical, vector in rows:
        pattern = rf"<tr><td>{size} operations</td><td>(\d+)%</td><td>(\d+)%</td></tr>"
        found = re.search(pattern, site)
        if not found:
            continue  # the site is allowed to carry less, not to carry it wrong
        assert found.groups() == (lexical, vector), (
            f"at {size} operations the README says {lexical}% / {vector}% and the site "
            f"says {found.group(1)}% / {found.group(2)}%"
        )


def test_the_site_build_is_at_least_valid_python(tmp_path):
    """The suite stayed green with a syntax error in `site/build.py`.

    On 2026-08-29 a bad edit left an `if` at the wrong indentation there, and
    1391 tests passed anyway: this file reads `build.py` as *text* — to check
    the sentences it emits — and nothing ever compiles it. The site is one of
    the four public surfaces, so its builder being broken is a broken showcase
    that only shows up when somebody deploys.

    Compiling is not building, and it deliberately stops short of running the
    thing: a real build takes minutes and needs the engines. What it buys is
    that the failure arrives from the test suite rather than from Pages.

    The byte-code goes into this test's own `tmp_path`, not next to the source.
    It used to write `build.pyc` beside `build.py` and delete it afterwards,
    which is a **shared path**: two runs of the suite over the same checkout
    raced on it, and on 2026-08-31 two independent parallel runs each produced a
    different failure, both green in isolation. Two runs at once is not a
    supported configuration, but a suite that goes red once on a shared checkout
    will do it again on a runner with retries — where it looks like a defect in
    the product rather than in the test. A test that writes outside `tmp_path`
    has to justify it, and this one could not.
    """
    import py_compile

    for script in (SITE_BUILD, ROOT / "benchmarks" / "worked_example.py"):
        try:
            py_compile.compile(
                str(script), doraise=True, cfile=str(tmp_path / f"{script.stem}.pyc")
            )
        except py_compile.PyCompileError as broken:  # pragma: no cover
            raise AssertionError(f"{script.name} does not compile: {broken}") from None


def test_the_manifest_on_the_front_page_conforms_to_the_specification():
    """The record a third-party implementer copies had none of the required fields.

    Eighty lines above it the page says "Records carry `spec_version`, and CI
    validates real MapSmith output against the spec's own validator" — and the
    only manifest a reader sees under that sentence carried no `spec_version`,
    no `producer` and no `verification`. It was hand-written and had drifted for
    two releases while every generated surface stayed correct.

    Validated against the specification's own validator, vendored from the
    published repository rather than reimplemented here.
    """
    import importlib.util

    blocks = re.findall(r"```json\n(.*?)\n```", README.read_text(encoding="utf-8"), re.DOTALL)
    manifests = [
        json.loads(block)
        for block in blocks
        if '"operation"' in block and '"engine"' in block
    ]
    assert manifests, (
        "the README no longer shows a manifest, or the block stopped looking like "
        "one — this guard would then pass on a page with nothing to check"
    )

    spec = importlib.util.spec_from_file_location(
        "spec_validator", ROOT / "tests" / "data" / "manifest_spec_validator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        (ROOT / "tests" / "data" / "manifest-v1.schema.json").read_text(encoding="utf-8")
    )
    checker = jsonschema.Draft202012Validator(schema)
    for manifest in manifests:
        # BOTH implementations. The schema is the normative one, and a page that
        # says "conforming" on the strength of the lenient one of two is the
        # sentence a reader trusts — a `pipeline: null` divergence between them
        # was live when this test was written.
        problems = module.problems(manifest) + [
            error.message for error in checker.iter_errors(manifest)
        ]
        assert problems == [], (
            f"the manifest on the front page is not a conforming record: "
            f"{problems}. It is what an implementer copies."
        )


def test_the_published_doi_is_the_concept_doi():
    """Two DOIs exist per release and only one is safe to write down.

    Zenodo mints a version DOI for each release and one concept DOI that always
    resolves to the newest. A version DOI hard-coded into a file nobody
    re-reads becomes a citation for a superseded release the moment the next one
    lands. The sibling repositories carry the same guard, which is how this one
    came to be written before it was needed here.
    """
    yaml = pytest.importorskip("yaml")

    readme = README.read_text(encoding="utf-8")
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))

    # The PARSED `doi` field, not a grep of the file. The first version searched
    # the text, so deleting the `doi:` line left the test green because the
    # string still appeared in the `identifiers:` block below it — and the
    # converters that turn a CFF into archive metadata read `doi`, not
    # `identifiers`: with the field removed, the DOI vanishes from their output
    # entirely. Measuring something adjacent to the claim, in a test written to
    # stop exactly that.
    declared = citation.get("doi")
    assert declared, (
        "CITATION.cff has no `doi` field. An `identifiers:` entry alone does "
        "not reach the converters, so the DOI would disappear from the archive "
        "metadata while still appearing in the file."
    )

    in_readme = set(re.findall(r"10\.5281/zenodo\.(\d+)", readme))
    assert in_readme, "the README publishes no DOI of its own"
    assert declared.split(".")[-1] in in_readme, (
        f"CITATION.cff declares {declared} and the README cites "
        f"zenodo.{sorted(in_readme)}"
    )
    # One DOI in the citation file, and it must be the concept one: a version
    # DOI written here becomes a citation for a superseded release at the next
    # tag.
    everywhere = set(re.findall(r"10\.5281/zenodo\.(\d+)",
                               (ROOT / "CITATION.cff").read_text(encoding="utf-8")))
    assert everywhere == {declared.split(".")[-1]}, (
        f"CITATION.cff mentions more than one DOI: {sorted(everywhere)}"
    )
