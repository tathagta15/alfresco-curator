"""Generate the architecture diagrams as Excalidraw scenes.

Diagrams drift the moment they are drawn by hand, so they are emitted from a
single description of the system. Open the generated `.excalidraw` files at
excalidraw.com (or the VS Code Excalidraw extension) to edit or export.

    python docs/diagrams/generate.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent

# Excalidraw's hand-drawn style needs a per-element seed; a fixed RNG keeps the
# output stable so regenerating does not churn the diff.
RNG = random.Random(20261009)

PALETTE = {
    "external": ("#1971c2", "#d0ebff"),  # things we do not own
    "service": ("#2f9e44", "#d3f9d8"),  # our processes
    "llm": ("#9c36b5", "#f3d9fa"),  # model calls
    "deterministic": ("#1098ad", "#c5f6fa"),  # probes, no model
    "decision": ("#e8590c", "#ffec99"),  # routers
    "store": ("#5c7cfa", "#dbe4ff"),
    "plain": ("#1e1e1e", "transparent"),
}

FONT = 2  # 1 hand-drawn, 2 normal, 3 code
LINE_HEIGHT = 1.25


def _base(kind: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {
        "id": f"{kind}-{RNG.getrandbits(48):x}",
        "type": kind,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": RNG.getrandbits(31),
        "version": 1,
        "versionNonce": RNG.getrandbits(31),
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
    }


def text(
    body: str, x: float, y: float, size: int = 16, colour: str = "#1e1e1e", align: str = "left"
) -> dict[str, Any]:
    lines = body.split("\n")
    width = max(len(line) for line in lines) * size * 0.58
    height = len(lines) * size * LINE_HEIGHT
    element = _base("text", x, y, width, height)
    element.update(
        {
            "strokeColor": colour,
            "text": body,
            "originalText": body,
            "fontSize": size,
            "fontFamily": FONT,
            "textAlign": align,
            "verticalAlign": "top",
            "containerId": None,
            "lineHeight": LINE_HEIGHT,
            "autoResize": True,
        }
    )
    return element


def box(
    label: str,
    x: float,
    y: float,
    w: float = 220,
    h: float = 72,
    style: str = "service",
    size: int = 16,
    dashed: bool = False,
) -> list[dict[str, Any]]:
    stroke, fill = PALETTE[style]
    rect = _base("rectangle", x, y, w, h)
    rect.update(
        {
            "strokeColor": stroke,
            "backgroundColor": fill,
            "roundness": {"type": 3},
            "strokeStyle": "dashed" if dashed else "solid",
        }
    )

    lines = label.split("\n")
    label_height = len(lines) * size * LINE_HEIGHT
    caption = text(label, x, y + (h - label_height) / 2, size=size, colour=stroke, align="center")
    caption.update(
        {
            "x": x,
            "width": w,
            "containerId": rect["id"],
            "textAlign": "center",
            "verticalAlign": "middle",
            "autoResize": False,
        }
    )
    rect["boundElements"] = [{"id": caption["id"], "type": "text"}]
    return [rect, caption]


def arrow(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    label: str = "",
    colour: str = "#1e1e1e",
    dashed: bool = False,
    label_at: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    element = _base("arrow", x1, y1, abs(x2 - x1), abs(y2 - y1))
    element.update(
        {
            "strokeColor": colour,
            "points": [[0, 0], [x2 - x1, y2 - y1]],
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "strokeStyle": "dashed" if dashed else "solid",
            "roundness": {"type": 2},
            "elbowed": False,
        }
    )
    out = [element]
    if label:
        lx, ly = label_at or ((x1 + x2) / 2 - len(label) * 3.4, (y1 + y2) / 2 - 20)
        out.append(text(label, lx, ly, size=12, colour=colour))
    return out


def elbow(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    label: str = "",
    colour: str = "#1e1e1e",
    dashed: bool = False,
    label_at: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """A right-angled connector, so branches never cut across other boxes."""
    element = _base("arrow", x1, y1, abs(x2 - x1), abs(y2 - y1))
    mid_x = x2 - x1
    element.update(
        {
            "strokeColor": colour,
            "points": [[0, 0], [mid_x, 0], [mid_x, y2 - y1]],
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "strokeStyle": "dashed" if dashed else "solid",
            "roundness": {"type": 2},
            "elbowed": False,
        }
    )
    out = [element]
    if label:
        lx, ly = label_at or (x1 + 12, y1 - 22)
        out.append(text(label, lx, ly, size=12, colour=colour))
    return out


def scene(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "alfresco-curator/docs/diagrams/generate.py",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def write(name: str, elements: list[dict[str, Any]]) -> None:
    path = OUT / f"{name}.excalidraw"
    path.write_text(json.dumps(scene(elements), indent=2), encoding="utf-8")
    print(f"wrote {path.relative_to(OUT.parent.parent)}")


# --------------------------------------------------------------- 1. system
def system_architecture() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []
    e.append(text("Alfresco Curator — deployment", 40, 24, size=24))
    e.append(
        text(
            "Nine containers. The Curator never writes to the repository;"
            " it reads, probes and explains.",
            40,
            60,
            size=14,
            colour="#5c5f66",
        )
    )

    e += box("Browser\nAlfresco Share UI", 40, 120, 200, 76, "external")
    e += box("Share 26.2\n:8180  (iframe host)", 300, 120, 220, 76, "external")
    e += box("Curator API\nFastAPI :8600", 300, 260, 220, 76, "service")
    e += box("LLM gateway\nOpenAI-compatible", 40, 260, 200, 76, "llm")

    e += box("Alfresco Repository\nACS 26.3  :8080", 620, 120, 240, 76, "external")
    e += box("Elasticsearch 8.17\n:9200", 620, 260, 240, 76, "store")
    e += box("Batch indexer 5.8\nactuator :8081", 620, 400, 240, 76, "external")
    e += box("ActiveMQ 6.2\nJolokia :8161", 620, 540, 240, 76, "external")
    e += box("PostgreSQL 17\n:5432", 940, 120, 200, 76, "store")
    e += box("Transform core AIO\n:8090", 940, 540, 200, 76, "external")

    e += arrow(240, 158, 300, 158, "HTTPS")
    e += arrow(410, 196, 410, 260, "iframe + ticket")
    e += arrow(300, 298, 240, 298, "translate / explain", PALETTE["llm"][0])

    e += arrow(520, 290, 620, 180, "REST v1  search, nodes, ACL")
    e += arrow(520, 300, 620, 300, "_doc, _analyze, _count", PALETTE["store"][0])
    e += arrow(520, 320, 620, 430, "/actuator/metrics")
    e += arrow(520, 330, 620, 570, "queue depth")

    e += arrow(860, 158, 940, 158, "JDBC")
    e += arrow(740, 400, 740, 336, "writes index", PALETTE["store"][0], label_at=(752, 352))
    # Routed down the left of Elasticsearch rather than through it.
    e += arrow(580, 196, 580, 438)
    e += arrow(580, 438, 620, 438)
    e.append(text("polls the DB\nevery 15s", 496, 300, size=12, colour="#1e1e1e"))
    e += arrow(640, 196, 580, 196)
    e += arrow(860, 578, 940, 578, "transform")

    e.append(
        text(
            "Indexing polls the database. ActiveMQ carries transform/rendition\n"
            "traffic only — a backlog there stops full text being extracted,\n"
            "but it is NOT on the indexing path.",
            620,
            650,
            size=13,
            colour="#c92a2a",
        )
    )

    e.append(text("Legend", 40, 400, size=16))
    for index, (style, caption) in enumerate(
        [
            ("service", "our service"),
            ("external", "external system"),
            ("store", "data store"),
            ("llm", "model call"),
        ]
    ):
        e += box(caption, 40, 432 + index * 56, 200, 44, style, size=13)
    return e


# ------------------------------------------------------- 2. agent state machine
def agent_state_machine() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []
    e.append(text("Agent — deterministic escalation ladder", 40, 24, size=24))
    e.append(
        text(
            "The controller is code, not the model."
            " Infrastructure is blamed only once the query is cleared.",
            40,
            60,
            size=14,
            colour="#5c5f66",
        )
    )

    col, w = 300, 320
    mid = col + w / 2

    e += box("START", mid - 60, 120, 120, 50, "plain", size=14)
    e += box("plan\ntranslate to AFTS / Lucene", col, 220, w, 76, "llm", size=14)
    e += box("search\nexecute against the repository", col, 350, w, 76, "deterministic", size=14)
    e += box("hits > 0 ?", col + 70, 480, 180, 60, "decision", size=15)
    e += box(
        "forensics\nclause reach · narrowing · path reach", col, 610, w, 76, "deterministic", 13
    )
    e += box("recover\nre-run without the dead clause", col, 740, w, 76, "deterministic", 13)
    e += box("dead clause or dead path ?", col + 20, 870, 280, 60, "decision", size=14)
    e += box("infra\npipeline telemetry sweep", col, 1000, w, 76, "deterministic", 14)

    # Every branch converges on one reporting column to the right.
    rep = 820
    e += box("report\nanswer  ·  postmortem", rep, 610, 300, 76, "llm", size=15)
    e += box("remember\nappend to thread history", rep, 740, 300, 68, "deterministic", 14)
    e += box("END", rep + 90, 870, 120, 50, "plain", size=14)

    e += arrow(mid, 170, mid, 220)
    e += arrow(mid, 296, mid, 350)
    e += arrow(mid, 426, mid, 480)
    e += arrow(mid, 540, mid, 610, "no — investigate", label_at=(mid + 14, 560))
    e += arrow(mid, 686, mid, 740)
    e += arrow(mid, 816, mid, 870)
    e += arrow(mid, 930, mid, 1000, "no — sweep the pipeline", label_at=(mid + 14, 950))

    e += elbow(col + 250, 510, rep, 648, "yes", label_at=(col + 268, 486))
    e += elbow(col + 300, 900, rep, 672, "yes — the query is at fault", label_at=(col + 312, 846))
    e += elbow(col + w, 1038, rep + 150, 686, "", PALETTE["deterministic"][0])

    e += arrow(rep + 150, 686, rep + 150, 740)
    e += arrow(rep + 150, 808, rep + 150, 870)

    e.append(
        text(
            "Only two nodes call the model, and both are handed evidence that\n"
            "was already gathered. The model narrates; it never decides what\n"
            "to probe, so the same question yields the same investigation.",
            820,
            960,
            size=13,
            colour="#9c36b5",
        )
    )
    e.append(
        text(
            "Cache: count::user::query and infra::sweep (30s TTL).\n"
            "Checkpoint: MemorySaver per thread_id for multi-turn memory.",
            820,
            1060,
            size=13,
            colour="#5c5f66",
        )
    )
    return e


# --------------------------------------------------- 3. diagnostic layers
def diagnostic_layers() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []
    e.append(text("Diagnose — why did this document not match?", 40, 24, size=24))
    e.append(
        text(
            "Input: the failing query plus the node that should have matched."
            " Every layer is a live probe.",
            40,
            60,
            size=14,
            colour="#5c5f66",
        )
    )

    e += box("Failing query  +  node id", 400, 110, 320, 60, "plain", size=16)
    e += arrow(560, 170, 560, 210)

    layers = [
        (
            "1 · Metadata",
            'Replay each clause scoped to the node\nID:"..." AND (clause)',
            "cm:description is STATUS:DRAFT,\nnot APPROVED",
        ),
        (
            "2 · Path boundary",
            "Probe the PATH clause against the node,\nand walk the folder back up",
            "node lives in /Engineering,\nquery asked for /Finance",
        ),
        (
            "3 · Permissions",
            "Indexed READER / DENIED vs the user's\nauthorities; parent folder ACL",
            "inheritance on, but only\nGROUP_ARCHITECTS granted",
        ),
        (
            "4 · Analyzer",
            "Elasticsearch _analyze on both the clause\nvalue and the stored value",
            "all terms present, phrase\norder is what fails",
        ),
        (
            "5 · Pipeline",
            "Indexer watermarks, ES document, broker\nbacklog — gated, not always run",
            "indexed copy older than\nthe repository version",
        ),
    ]
    for index, (title, how, finding) in enumerate(layers):
        y = 220 + index * 110
        e += box(title, 60, y, 190, 80, "deterministic", size=15)
        e += box(how, 290, y, 400, 80, "plain", size=13)
        e += box(finding, 730, y, 330, 80, "decision", size=13)
        if index:
            e += arrow(155, y - 30, 155, y)

    e += arrow(560, 760, 560, 800)
    e += box("Grounded report\nroot cause · evidence · fix", 400, 800, 320, 76, "llm", size=15)

    e.append(
        text(
            "Layer 5 is gated by pipeline_is_implicated(): tracing costs several\n"
            "round trips, so it runs only when the query has been cleared.",
            60,
            800,
            size=13,
            colour="#c92a2a",
        )
    )
    e.append(
        text(
            "The model receives these findings.\nIt never produces them.",
            760,
            800,
            size=13,
            colour="#9c36b5",
        )
    )
    return e


# ------------------------------------------------------------ 4. search flow
def search_flow() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []
    e.append(text("Search — natural language to a repository query", 40, 24, size=24))
    e.append(
        text(
            "The query language is detected from the deployment,"
            " never chosen by whoever is searching.",
            40,
            60,
            size=14,
            colour="#5c5f66",
        )
    )

    steps = [
        ("Plain English prompt", "plain"),
        ("detect_query_language()\nask the repository once, then cache", "deterministic"),
        ("generate_query()\nmodel writes AFTS or Lucene", "llm"),
        ("path_problems() + advisories()\nstructural repair, one retry", "deterministic"),
        ("search_afts()\nexecute against the repository", "deterministic"),
    ]
    for index, (label, style) in enumerate(steps):
        y = 120 + index * 100
        e += box(label, 60, y, 420, 76, style, size=14)
        if index:
            e += arrow(270, y - 24, 270, y)

    e += box("hits ?", 560, 420, 140, 60, "decision", size=15)
    e += arrow(480, 458, 560, 458)

    e += box("Result cards\nthumbnail · title · status · Open", 760, 300, 340, 80, "plain", 14)
    e += arrow(700, 440, 760, 380, "found")

    e += box(
        "Zero-hit forensics\n"
        "clause reach — which term matches nothing\n"
        "narrowing — which combination empties it\n"
        "path reach — which folder step is wrong",
        760,
        430,
        340,
        120,
        "deterministic",
        13,
    )
    e += arrow(700, 470, 760, 480, "none")

    e.append(
        text(
            "A zero-hit search is the interesting case: it is otherwise\n"
            "indistinguishable from an empty repository.",
            760,
            570,
            size=13,
            colour="#1098ad",
        )
    )
    return e


if __name__ == "__main__":
    write("01-system-architecture", system_architecture())
    write("02-agent-state-machine", agent_state_machine())
    write("03-diagnostic-layers", diagnostic_layers())
    write("04-search-flow", search_flow())
