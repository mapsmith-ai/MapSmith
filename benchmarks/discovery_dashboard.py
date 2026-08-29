"""A page for the part of discovery that a person has to do.

    python benchmarks/discovery_dashboard.py                       # measurements only
    python benchmarks/discovery_dashboard.py --log discovery.jsonl # plus recorded cases
    python benchmarks/discovery_dashboard.py --out /tmp/page.html

Everything about the discovery layer that a machine can decide is already
decided by a machine, recomputed from this repository by
`benchmarks/discovery_report.py`. What is left needs a human and has, until now,
been a text dump: reading a recorded case and saying which operation was right,
and settling the requests where the two model labellers disagree with each
other. That second one is not a chore — it is the open question the published
percentages rest on, because two labellers agreeing 70% of the time is the
ceiling of a task that has no single right answer.

So this writes one self-contained HTML file: the measurements, every recorded
case with the candidate list it was chosen from, every request the labellers
split on, and a control to answer them. Answers live in the browser's own
storage and come back out as JSON you paste into the benchmark. Nothing is
uploaded, no script is fetched, and the file works with the network off — the
same reason the ranker is local.

**The numbers move as you answer.** Each panel recomputes over what has been
labelled and shows it beside the published figure, so the effect of replacing
model labels with human ones is visible while you work rather than after.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

import discovery_report  # noqa: E402

from mapsmith import catalog  # noqa: E402


def catalog_rows() -> list[dict[str, Any]]:
    """Every operation, with the text a person picks by rather than searches by."""
    return [
        {
            "name": op["name"],
            "category": op["category"],
            "produces": op["produces"],
            "status": op["status"],
            "summary": op["summary"],
            "distinguishes": op.get("distinguishes", ""),
            "inputs": op["applicability"]["inputs"],
            "dataset_inputs": op["applicability"].get("dataset_inputs"),
        }
        for op in catalog.OPERATIONS
    ]


def recorded_cases(path: Path | None) -> list[dict[str, Any]]:
    """The discovery log, one entry per search, unattributed searches included.

    A search nothing followed is kept rather than dropped: it is a request the
    catalog did not serve, which is the more interesting half.
    """
    if path is None or not path.exists():
        return []
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        cases.append(
            {
                "at": record.get("at"),
                "query": record.get("query", ""),
                "declared": record.get("declared", {}),
                "engine": record.get("engine"),
                "status": record.get("status"),
                "delivered": record.get("delivered", []),
                "chose": record.get("chose"),
                "position": record.get("position_of_choice"),
                "searches_ago": record.get("searches_ago"),
            }
        )
    return cases


def disagreements(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The requests the two labellers answered differently.

    Both-unanswerable pairs are excluded (agreement on "no" is agreement) and so
    are pairs where they named the same operation. What is left is the 30% that
    D-054 says needs somebody who has done the job: either one of them is wrong,
    or both answers are defensible and the request has no single right answer —
    and those two cases look identical from here.
    """
    names = {op["name"] for op in catalog.OPERATIONS}
    split = []
    for q in queries:
        claude, gemini = q.get("label_claude"), q.get("label_gemini")
        if not claude or not gemini or claude == gemini:
            continue
        # A label naming an operation that no longer exists is a stale row, not
        # a disagreement about geography.
        stale = [
            name
            for name in (claude, gemini)
            if name not in names and name not in discovery_report.NOT_AN_OPERATION
        ]
        answer = catalog.search(q["query"], limit=10, engine="lexical")
        split.append(
            {
                "query": q["query"],
                "scenario": q.get("scenario", ""),
                "claude": claude,
                "gemini": gemini,
                "stale": stale,
                "ranked": [entry["name"] for entry in catalog.entries(answer)][:10],
            }
        )
    return split


def measurements() -> dict[str, Any]:
    """Everything `discovery_report` computes, in the shape the page draws."""
    queries = discovery_report.load()
    answerable = discovery_report.answerable(queries)
    agree = discovery_report.agreement(queries)
    lexical = discovery_report.ablation(answerable, engine="lexical")
    try:
        vector = discovery_report.ablation(answerable, engine="vector")
    except Exception as failure:  # noqa: BLE001 - no model, no column, said so
        vector = []
        print(f"note: the embedding engine could not be loaded ({failure}); "
              "the page will carry the BM25 column alone", file=sys.stderr)
    return {
        "requests": len(queries),
        "answerable": len(answerable),
        "agreement_all": agree["all"],
        "agreement_answerable": agree["answerable"],
        "ablation_lexical": lexical,
        "ablation_vector": vector,
        "catalog_size": len(catalog.OPERATIONS),
    }


def build(log: Path | None) -> str:
    queries = discovery_report.load()
    data = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "catalog": catalog_rows(),
        "measurements": measurements(),
        "recorded": recorded_cases(log),
        "disagreements": disagreements(queries),
        "log_path": str(log) if log else None,
    }
    blob = json.dumps(data, ensure_ascii=False)
    # `</script>` inside a JSON string would close the tag early. Escaping the
    # angle bracket is enough and keeps the JSON valid.
    blob = blob.replace("<", "\\u003c")
    return TEMPLATE.replace("__DATA__", blob)


def main(argv: list[str] | None = None) -> int:
    # cp1252 consoles raise on the em dash below rather than degrading.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=None,
                        help="a file written by MAPSMITH_DISCOVERY_LOG")
    parser.add_argument("--out", type=Path, default=Path("discovery_dashboard.html"))
    args = parser.parse_args(argv)

    if args.log and not args.log.exists():
        print(f"no log at {args.log}", file=sys.stderr)
        return 1
    args.out.write_text(build(args.log), encoding="utf-8")
    size = args.out.stat().st_size // 1024
    print(f"{args.out} ({size} KB) — open it in a browser")
    return 0


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MapSmith discovery — what the machine cannot decide</title>
<style>
:root {
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1a1a1a; --quiet: #6b6b6b;
  --line: #e2ded8; --accent: #1f6f5c; --warn: #a8541b; --pick: #e8f2ef;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181a; --panel: #1e2124; --ink: #e8e6e3; --quiet: #9a9a9a;
    --line: #33383d; --accent: #63b39c; --warn: #d9925c; --pick: #21332e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 60rem; margin: 0 auto; padding: 2rem 1.25rem 6rem; }
h1 { font-size: 1.6rem; margin: 0 0 .3rem; letter-spacing: -.01em; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .5rem; }
h3 { font-size: .95rem; margin: 1.5rem 0 .4rem; }
p { margin: .5rem 0; }
.quiet { color: var(--quiet); font-size: .88rem; }
code, .mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .86em; }
.panel {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.15rem; margin: 1rem 0;
}
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line); }
th { font-weight: 600; color: var(--quiet); font-size: .8rem; text-transform: uppercase;
     letter-spacing: .04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }
.tally { display: flex; flex-wrap: wrap; gap: 1.5rem; margin: .75rem 0; }
.tally div { min-width: 7rem; }
.tally .big { font-size: 1.7rem; font-variant-numeric: tabular-nums; line-height: 1.1; }
.tally .cap { font-size: .78rem; color: var(--quiet); }
.case { border-top: 1px solid var(--line); padding: 1rem 0; }
.case:first-of-type { border-top: none; }
.q { font-size: 1.02rem; margin: 0 0 .35rem; }
.meta { font-size: .8rem; color: var(--quiet); margin-bottom: .5rem; }
.chips { display: flex; flex-wrap: wrap; gap: .35rem; margin: .4rem 0; }
.chip {
  border: 1px solid var(--line); background: transparent; color: var(--ink);
  border-radius: 999px; padding: .2rem .6rem; font-size: .82rem; cursor: pointer;
  font-family: ui-monospace, Consolas, monospace;
}
.chip:hover { border-color: var(--accent); }
.chip.ran { border-color: var(--warn); }
.chip.picked { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip.claude::after, .chip.gemini::after { font-family: inherit; font-size: .72em;
  color: var(--quiet); margin-left: .35rem; }
.chip.claude::after { content: "claude"; }
.chip.gemini::after { content: "gemini"; }
.chip.picked::after { color: rgba(255,255,255,.75); }
select, input[type=search], textarea {
  font: inherit; background: var(--panel); color: var(--ink);
  border: 1px solid var(--line); border-radius: 6px; padding: .3rem .45rem;
}
textarea { width: 100%; min-height: 12rem; font-family: ui-monospace, Consolas, monospace;
  font-size: .8rem; }
button.action {
  font: inherit; background: var(--accent); color: #fff; border: 0;
  border-radius: 6px; padding: .4rem .9rem; cursor: pointer;
}
button.ghost { background: transparent; color: var(--quiet); border: 1px solid var(--line); }
.bar { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; margin: .75rem 0; }
.done { color: var(--accent); font-weight: 600; }
.empty { color: var(--quiet); font-style: italic; }
</style>
</head>
<body>
<main>
<h1>Discovery — the part a machine cannot decide</h1>
<p class="quiet" id="subtitle"></p>

<div class="panel">
  <p><strong>Everything here is local.</strong> This file was written by
  <code>benchmarks/discovery_dashboard.py</code> from your own checkout; the queries in it
  are yours, nothing is uploaded, no script is fetched, and it works offline. Your answers
  live in this browser's storage until you export them.</p>
</div>

<h2>Where the two labellers disagree</h2>
<p>Every published percentage rests on 155 requests labelled by two language models. Where
they name the same operation, the row is as good as it gets without users. Where they do
not, one of them is wrong <em>or</em> both answers are defensible — and those look identical
from here. Answering these is what turns a number called <em>agreement</em> into one that
has seen a person who has done the job.</p>

<div class="panel">
  <div class="tally">
    <div><div class="big" id="d-total">–</div><div class="cap">split rows</div></div>
    <div><div class="big" id="d-done">–</div><div class="cap">you answered</div></div>
    <div><div class="big" id="d-claude">–</div><div class="cap">you agree with claude</div></div>
    <div><div class="big" id="d-gemini">–</div><div class="cap">you agree with gemini</div></div>
    <div><div class="big" id="d-neither">–</div><div class="cap">neither of them</div></div>
  </div>
  <p class="quiet" id="d-note"></p>
</div>
<div id="disagreements"></div>

<h2>Recorded from use</h2>
<p id="rec-intro"></p>
<div class="panel" id="rec-panel">
  <div class="tally">
    <div><div class="big" id="r-total">–</div><div class="cap">searches</div></div>
    <div><div class="big" id="r-ran">–</div><div class="cap">a run followed</div></div>
    <div><div class="big" id="r-first">–</div><div class="cap">chosen was ranked 1st</div></div>
    <div><div class="big" id="r-three">–</div><div class="cap">in the top three</div></div>
    <div><div class="big" id="r-labelled">–</div><div class="cap">you answered</div></div>
  </div>
  <p class="quiet" id="r-note"></p>
</div>
<div id="recorded"></div>

<h2>What the arithmetic already says</h2>
<p>Recomputed from this checkout by <code>benchmarks/discovery_report.py</code>, over the
<span id="m-answerable">–</span> requests both labellers placed on an operation that still
exists. <em>Delivered</em> is not an accuracy figure: below the choose threshold every
survivor is handed over, so it measures whether narrowing ever drops the answer.</p>
<div class="panel scroll">
  <table id="ablation"></table>
</div>
<div class="panel">
  <div class="tally">
    <div><div class="big" id="m-ceiling">–</div><div class="cap">the two labellers agree</div></div>
    <div><div class="big" id="m-catalog">–</div><div class="cap">operations in the catalog</div></div>
    <div><div class="big" id="m-requests">–</div><div class="cap">requests in the set</div></div>
  </div>
  <p class="quiet">Both ranking columns are shown because the shipped default picks between
  the engines by whether a model loads — one unnamed column would be a measurement of the
  machine, which it once was.</p>
</div>

<h2>Take your answers out</h2>
<p>Rows in the shape of <code>tests/data/discovery_queries.json</code>, with your answer as
<code>label_human</code>. Read them, then paste the ones you stand behind.</p>
<div class="bar">
  <button class="action" id="export">Show the JSON</button>
  <button class="ghost" id="copy">Copy</button>
  <button class="ghost" id="forget">Forget my answers</button>
  <span id="saved" class="quiet"></span>
</div>
<textarea id="out" spellcheck="false" placeholder="nothing answered yet"></textarea>
</main>

<script type="application/json" id="data">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("data").textContent);
const KEY = "mapsmith-discovery-labels-v1";

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || "{}"); }
  catch (e) { return {}; }
}
function save(state) {
  try { localStorage.setItem(KEY, JSON.stringify(state)); }
  catch (e) { note("this browser refused to store the answers — export before you leave"); }
}
function note(text) { document.getElementById("saved").textContent = text; }
let LABELS = load();

const byName = new Map(DATA.catalog.map(op => [op.name, op]));
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function pct(part, whole) { return whole ? Math.round(100 * part / whole) + "%" : "–"; }

/* ---- the picker: chips for the obvious candidates, a full list behind them ---- */
function picker(key, suggested, marks, onPick) {
  const wrap = el("div");
  const chips = el("div", "chips");
  const draw = () => {
    chips.innerHTML = "";
    const chosen = LABELS[key] ? LABELS[key].label : null;
    const shown = suggested.slice();
    if (chosen && !shown.includes(chosen) && chosen !== "none" && chosen !== "ambiguous") {
      shown.push(chosen);
    }
    for (const name of shown) {
      const chip = el("button", "chip " + (marks[name] || ""), name);
      if (chosen === name) chip.classList.add("picked");
      chip.title = (byName.get(name) || {}).summary || "";
      chip.onclick = () => { pick(key, name); draw(); onPick(); };
      chips.appendChild(chip);
    }
    for (const special of ["none", "ambiguous"]) {
      const chip = el("button", "chip", special);
      if (chosen === special) chip.classList.add("picked");
      chip.title = special === "none"
        ? "MapSmith has no operation for this request"
        : "two answers are equally defensible";
      chip.onclick = () => { pick(key, special); draw(); onPick(); };
      chips.appendChild(chip);
    }
  };
  const all = el("select");
  all.appendChild(new Option("something else…", ""));
  for (const op of DATA.catalog) {
    all.appendChild(new Option(op.name + " — " + op.summary.slice(0, 70), op.name));
  }
  all.onchange = () => { if (all.value) { pick(key, all.value); draw(); onPick(); all.value = ""; } };
  draw();
  wrap.appendChild(chips);
  wrap.appendChild(all);
  return wrap;
}
function pick(key, label) {
  if (LABELS[key] && LABELS[key].label === label) delete LABELS[key];
  else LABELS[key] = { label: label };
  save(LABELS);
  note("saved in this browser");
}

/* ---- panel 1: the split rows ---- */
function drawDisagreements() {
  const host = document.getElementById("disagreements");
  host.innerHTML = "";
  if (!DATA.disagreements.length) {
    host.appendChild(el("p", "empty", "the two labellers agreed on everything"));
    return;
  }
  for (const row of DATA.disagreements) {
    const key = "d:" + row.query;
    const box = el("div", "case");
    box.appendChild(el("p", "q", row.query));
    const meta = el("p", "meta");
    meta.textContent = row.scenario ? "scenario: " + row.scenario : "";
    if (row.stale.length) {
      meta.textContent += (meta.textContent ? " · " : "") +
        "names an operation that no longer exists: " + row.stale.join(", ");
    }
    box.appendChild(meta);
    const marks = {};
    marks[row.claude] = "claude";
    marks[row.gemini] = "gemini";
    const suggested = [row.claude, row.gemini].filter(n => byName.has(n));
    for (const name of row.ranked) {
      if (!suggested.includes(name) && suggested.length < 8) suggested.push(name);
    }
    box.appendChild(picker(key, suggested, marks, tallyDisagreements));
    host.appendChild(box);
  }
}
function tallyDisagreements() {
  const rows = DATA.disagreements;
  let done = 0, claude = 0, gemini = 0, neither = 0;
  for (const row of rows) {
    const answer = LABELS["d:" + row.query];
    if (!answer) continue;
    done++;
    if (answer.label === row.claude) claude++;
    else if (answer.label === row.gemini) gemini++;
    else neither++;
  }
  document.getElementById("d-total").textContent = rows.length;
  document.getElementById("d-done").textContent = done;
  document.getElementById("d-claude").textContent = done ? pct(claude, done) : "–";
  document.getElementById("d-gemini").textContent = done ? pct(gemini, done) : "–";
  document.getElementById("d-neither").textContent = done ? pct(neither, done) : "–";
  const m = DATA.measurements;
  const ceiling = Math.round(100 * m.agreement_answerable[0] / m.agreement_answerable[1]);
  document.getElementById("d-note").textContent = done
    ? ("Over the " + done + " you have answered, the two models were right about "
       + pct(claude + gemini, done) + " of the split rows between them and neither had it "
       + pct(neither, done) + " of the time. The published ceiling — how often they agree "
       + "with EACH OTHER over the answerable set — is " + ceiling + "%.")
    : ("Nothing answered yet. The published ceiling — how often the two models agree with "
       + "each other over the answerable set — is " + ceiling + "%, and these are the rows "
       + "that number is missing.");
}

/* ---- panel 2: the recorded cases ---- */
function drawRecorded() {
  const host = document.getElementById("recorded");
  const intro = document.getElementById("rec-intro");
  host.innerHTML = "";
  if (!DATA.recorded.length) {
    intro.innerHTML = "Nothing recorded yet. Set <code>MAPSMITH_DISCOVERY_LOG</code> to a "
      + "file path, work normally, then regenerate this page with <code>--log</code>. "
      + "Every search is written with the operation run after it, so the rows below become "
      + "cases written by use rather than by a model.";
    document.getElementById("rec-panel").style.display = "none";
    return;
  }
  intro.textContent = "One row per catalog search, with the operation run after it. A choice "
    + "the ranking did not put first is the cheapest correction available — but it can also "
    + "be the caller's mistake, so the flag is a question, not a verdict.";
  for (const row of DATA.recorded) {
    const key = "r:" + row.query;
    const box = el("div", "case");
    box.appendChild(el("p", "q", row.query));
    const bits = [];
    if (row.at) bits.push(row.at.replace("T", " ").replace("+00:00", "Z"));
    const declared = Object.entries(row.declared || {})
      .map(([k, v]) => k + "=" + v).join(", ");
    bits.push(declared ? "declared " + declared : "declared nothing");
    bits.push(row.delivered.length + " delivered");
    if (row.engine) bits.push("ranked by " + row.engine);
    if (row.status) bits.push("status " + row.status);
    if (row.chose) {
      bits.push("ran " + row.chose + (row.position ? " (position " + row.position + ")" : ""));
      if (row.position > 3) bits.push("NOT in the top three");
    } else {
      bits.push("nothing was run");
    }
    if (row.searches_ago) bits.push("attributed " + row.searches_ago + " searches back");
    box.appendChild(el("p", "meta", bits.join(" · ")));
    const marks = {};
    if (row.chose) marks[row.chose] = "ran";
    box.appendChild(picker(key, row.delivered.slice(0, 12), marks, tallyRecorded));
    host.appendChild(box);
  }
}
function tallyRecorded() {
  const rows = DATA.recorded;
  const ran = rows.filter(r => r.chose);
  const first = ran.filter(r => r.position === 1).length;
  const three = ran.filter(r => r.position && r.position <= 3).length;
  let labelled = 0, agreeFirst = 0, agreeThree = 0, delivered = 0;
  for (const row of rows) {
    const answer = LABELS["r:" + row.query];
    if (!answer) continue;
    labelled++;
    const at = row.delivered.indexOf(answer.label) + 1;
    if (at === 1) agreeFirst++;
    if (at >= 1 && at <= 3) agreeThree++;
    if (at >= 1) delivered++;
  }
  document.getElementById("r-total").textContent = rows.length;
  document.getElementById("r-ran").textContent = ran.length;
  document.getElementById("r-first").textContent = ran.length ? pct(first, ran.length) : "–";
  document.getElementById("r-three").textContent = ran.length ? pct(three, ran.length) : "–";
  document.getElementById("r-labelled").textContent = labelled;
  document.getElementById("r-note").textContent = labelled
    ? ("Against YOUR answers on those " + labelled + ": the ranking had it first "
       + pct(agreeFirst, labelled) + " of the time, in the top three "
       + pct(agreeThree, labelled) + ", and delivered it at all "
       + pct(delivered, labelled) + ". The first two are the ranking; the third is the "
       + "narrowing, and it is the one the design promises.")
    : ("The percentages above are about what was RUN, which is a choice somebody made and "
       + "not necessarily the right one. Answer a few and the same three numbers appear "
       + "against your answers instead.");
}

/* ---- the measurements ---- */
function drawMeasurements() {
  const m = DATA.measurements;
  const table = document.getElementById("ablation");
  const head = el("tr");
  for (const label of ["what the caller declares", "candidates left", "BM25 @3",
                       "embeddings @3", "delivered"]) {
    const th = el("th", label === "what the caller declares" ? "" : "num", label);
    head.appendChild(th);
  }
  table.appendChild(head);
  m.ablation_lexical.forEach((row, index) => {
    const vector = m.ablation_vector[index];
    const tr = el("tr");
    tr.appendChild(el("td", "", row.declared));
    tr.appendChild(el("td", "num", String(row.candidates)));
    tr.appendChild(el("td", "num", row.found_at_3 + "%"));
    tr.appendChild(el("td", "num", vector ? vector.found_at_3 + "%" : "–"));
    tr.appendChild(el("td", "num", row.delivered + "%"));
    table.appendChild(tr);
  });
  document.getElementById("m-answerable").textContent = m.answerable;
  document.getElementById("m-requests").textContent = m.requests;
  document.getElementById("m-catalog").textContent = m.catalog_size;
  document.getElementById("m-ceiling").textContent =
    Math.round(100 * m.agreement_answerable[0] / m.agreement_answerable[1]) + "%";
  document.getElementById("subtitle").textContent =
    "generated " + DATA.generated + " · " + m.catalog_size + " operations · "
    + DATA.recorded.length + " recorded searches · " + DATA.disagreements.length
    + " rows the labellers split on";
}

/* ---- export ---- */
function rows() {
  const out = [];
  for (const row of DATA.disagreements) {
    const answer = LABELS["d:" + row.query];
    if (!answer) continue;
    out.push({
      query: row.query, scenario: row.scenario, generated_by: "dashboard, answered by hand",
      split: "tune", label_human: answer.label,
      label_claude: row.claude, label_gemini: row.gemini
    });
  }
  for (const row of DATA.recorded) {
    const answer = LABELS["r:" + row.query];
    if (!answer) continue;
    out.push({
      query: row.query, scenario: "recorded from use",
      generated_by: "discovery log, answered by hand", split: "tune",
      label_human: answer.label, ran: row.chose,
      declared: row.declared, delivered_position: row.delivered.indexOf(answer.label) + 1 || null
    });
  }
  return out;
}
document.getElementById("export").onclick = () => {
  const list = rows();
  document.getElementById("out").value = list.length
    ? list.map(r => JSON.stringify(r)).join(",\\n")
    : "nothing answered yet";
};
document.getElementById("copy").onclick = async () => {
  const text = document.getElementById("out").value;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); note("copied"); }
  catch (e) { document.getElementById("out").select(); note("select and copy"); }
};
document.getElementById("forget").onclick = () => {
  if (!confirm("Delete every answer stored in this browser?")) return;
  LABELS = {};
  save(LABELS);
  drawDisagreements(); drawRecorded(); tallyDisagreements(); tallyRecorded();
  document.getElementById("out").value = "";
  note("forgotten");
};

drawMeasurements();
drawDisagreements();
drawRecorded();
tallyDisagreements();
tallyRecorded();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
