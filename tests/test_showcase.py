"""The front door has to lead everywhere, and say only true things.

The benchmark results were public for a day and nobody could find them: no
link from the README. Writing something and publishing something are not the
same act, and the difference is invisible to every other test in this suite.
These checks are mechanical on purpose — taste cannot be automated, but
reachability, dead links and stale numbers can.
"""

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
    """A tool table on the published page outlives the tool it describes."""
    from mapsmith import server

    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    table = re.search(
        r'<table class="tools".*?</table>', SITE_TEMPLATE.read_text(encoding="utf-8"), re.DOTALL
    )
    assert table, "the site template lost its tool table"
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", table.group(0)))
    ghosts = sorted(named - registered)
    assert not ghosts, (
        f"mapsmith.dev advertises tools that are not registered: {ghosts}. "
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
    manifest from the previous release stays on display forever."""
    from mapsmith import __version__

    text = _notebook_output_text(notebook)
    shown = set(re.findall(r'"mapsmith_version":\s*"([^"]+)"', text))
    assert shown <= {__version__}, (
        f"{notebook.name} displays manifests from {sorted(shown)} but this is "
        f"{__version__} — re-run the notebook instead of editing its output"
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
DRIVER_IDENTIFIERS = ("ESRI Shapefile", "ESRIJSON")


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


def test_no_vendor_is_named_in_public():
    """Silence about a named vendor is a decision, so it needs a check.

    A prose mention costs nothing to add and cannot be taken back once it is in
    a public git history, which is exactly the shape of mistake a test should
    be catching instead of a reviewer. One test over every tracked file rather
    than one per file: the whole list of offenders in a single failure is what
    a person fixing this wants to see."""
    offenders = {}
    for page in _tracked_text_files():
        if page.resolve() == Path(__file__).resolve():
            continue  # this file names them in order to forbid them
        text = page.read_text(encoding="utf-8", errors="replace")
        for identifier in DRIVER_IDENTIFIERS:
            text = text.replace(identifier, "")
        found = sorted({m.group(0) for m in VENDOR_SILENCE.finditer(text)})
        if found:
            offenders[str(page.relative_to(ROOT)).replace("\\", "/")] = found
    assert not offenders, (
        f"these files name a vendor we stay silent about in public: {offenders}. "
        "Say the thing without the name, or drop the sentence."
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
