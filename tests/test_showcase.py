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
    "page", sorted(p for p in [README, *(ROOT / "docs").glob("*.md")] if p.exists()),
    ids=lambda p: p.name,
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
    shipped = {"WhiteboxTools": "whitebox", "DuckDB": "duckdb", "exactextract": "exactextract"}
    coming = re.search(r"more to come:([^)]*)\)", text)
    if not coming:
        return
    wrong = [name for name, pkg in shipped.items() if pkg in all_deps and name in coming.group(1)]
    assert not wrong, f"listed as future work but already a dependency: {wrong}"
