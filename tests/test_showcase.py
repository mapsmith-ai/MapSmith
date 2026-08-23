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

    # regex, not tomllib: that is stdlib only from 3.11 and this suite runs on
    # the minimum supported Python (3.10)
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
    # read as text, not via tomllib: that is stdlib only from 3.11 and this
    # suite has to run on the minimum supported Python (3.10)
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
    testo = page.read_text(encoding="utf-8")
    # Code fences are already safe, and stripping them keeps the check honest
    # rather than making authors escape things twice.
    fuori_dal_codice = re.sub(r"```.*?```", "", testo, flags=re.DOTALL)
    colpevoli = [
        m.group(0)
        for m in BARE_URL.finditer(fuori_dal_codice)
        if any(host in m.group(0) for host in ILLUSTRATIVE_HOSTS)
    ]
    assert not colpevoli, (
        f"{page.name}: illustrative URLs rendered as clickable links — wrap them in "
        f"backticks so a reader does not follow one and find nothing: {colpevoli}"
    )
