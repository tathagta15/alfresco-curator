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


def line(
    points: list[tuple[float, float]], colour: str = "#1e1e1e", dashed: bool = False
) -> list[dict[str, Any]]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    element = _base("line", xs[0], ys[0], max(xs) - min(xs), max(ys) - min(ys))
    element.update(
        {
            "strokeColor": colour,
            "points": [[px - xs[0], py - ys[0]] for px, py in points],
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": None,
            "strokeStyle": "dashed" if dashed else "solid",
            "roundness": None,
        }
    )
    return [element]


# ------------------------------------------------------------- sequence scenes
SEQ_TOP = 130
SEQ_ROW = 70
SEQ_PITCH = 320
SEQ_ACTOR_W = 240

KIND_COLOUR = {
    "call": "#1e1e1e",
    "return": "#5c5f66",
    "model": PALETTE["llm"][0],
    "data": PALETTE["store"][0],
}


def sequence(
    title: str,
    subtitle: str,
    actors: list[tuple[str, str]],
    messages: list[tuple[int, int, str, str]],
    footnote: str = "",
) -> list[dict[str, Any]]:
    """Lifelines and messages, drawn from the same primitives as the other scenes."""
    e: list[dict[str, Any]] = [
        text(title, 40, 24, size=24),
        text(subtitle, 40, 60, size=14, colour="#5c5f66"),
    ]

    centres: list[float] = []
    for index, (label, style) in enumerate(actors):
        x = 40 + index * SEQ_PITCH
        e += box(label, x, SEQ_TOP, SEQ_ACTOR_W, 64, style, size=14)
        centres.append(x + SEQ_ACTOR_W / 2)

    head = SEQ_TOP + 64
    bottom = head + SEQ_ROW * (len(messages) + 1)
    for centre in centres:
        e += line([(centre, head), (centre, bottom)], colour="#adb5bd", dashed=True)

    for index, (src, dst, label, kind) in enumerate(messages):
        y = head + SEQ_ROW * (index + 1)
        colour = KIND_COLOUR.get(kind, "#1e1e1e")
        dashed = kind == "return"
        lines = label.split("\n")
        width = max(len(line_text) for line_text in lines) * 12 * 0.58

        if src == dst:
            centre = centres[src]
            e += line([(centre, y), (centre + 90, y), (centre + 90, y + 28)], colour=colour)
            e += arrow(centre + 90, y + 28, centre + 4, y + 28, colour=colour)
            e.append(text(label, centre + 104, y + 4, size=12, colour=colour))
            continue

        x1, x2 = centres[src], centres[dst]
        e += arrow(x1, y, x2, y, colour=colour, dashed=dashed)
        e.append(
            text(
                label,
                (x1 + x2) / 2 - width / 2,
                y - 14 - len(lines) * 12 * LINE_HEIGHT,
                size=12,
                colour=colour,
            )
        )

    if footnote:
        e.append(text(footnote, 40, bottom + 30, size=13, colour="#c92a2a"))
    return e


# ----------------------------------------------------------------- SVG export
# GitHub renders SVG inside Markdown but will not render .excalidraw JSON, so
# every scene is written both ways: the JSON stays editable, the SVG is what the
# README shows. Excalidraw's hand-drawn roughness is not reproduced here.
_SVG_FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def _esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _abs_points(element: dict[str, Any]) -> list[tuple[float, float]]:
    return [(element["x"] + px, element["y"] + py) for px, py in element["points"]]


def _bounds(elements: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for element in elements:
        if element["type"] in ("arrow", "line"):
            for px, py in _abs_points(element):
                xs.append(px)
                ys.append(py)
        else:
            xs += [element["x"], element["x"] + element["width"]]
            ys += [element["y"], element["y"] + element["height"]]
    return min(xs), min(ys), max(xs), max(ys)


def _svg_element(element: dict[str, Any]) -> str:
    stroke = element["strokeColor"]
    dash = ' stroke-dasharray="8 6"' if element.get("strokeStyle") == "dashed" else ""

    if element["type"] == "rectangle":
        fill = element["backgroundColor"]
        return (
            f'<rect x="{element["x"]:.1f}" y="{element["y"]:.1f}" '
            f'width="{element["width"]:.1f}" height="{element["height"]:.1f}" '
            f'rx="{12 if element.get("roundness") else 0}" '
            f'fill="{"none" if fill == "transparent" else fill}" stroke="{stroke}" '
            f'stroke-width="{element["strokeWidth"]}"{dash}/>'
        )

    if element["type"] in ("arrow", "line"):
        points = " ".join(f"{px:.1f},{py:.1f}" for px, py in _abs_points(element))
        head = f' marker-end="url(#a{stroke.lstrip("#")})"' if element.get("endArrowhead") else ""
        return (
            f'<polyline points="{points}" fill="none" stroke="{stroke}" '
            f'stroke-width="{element["strokeWidth"]}" stroke-linecap="round" '
            f'stroke-linejoin="round"{dash}{head}/>'
        )

    if element["type"] == "text":
        size = element["fontSize"]
        anchor = {"center": "middle", "right": "end"}.get(element["textAlign"], "start")
        if anchor == "middle":
            x = element["x"] + element["width"] / 2
        elif anchor == "end":
            x = element["x"] + element["width"]
        else:
            x = element["x"]
        spans = []
        for index, body in enumerate(element["text"].split("\n")):
            y = element["y"] + size * 0.92 + index * size * LINE_HEIGHT
            spans.append(
                f'<text x="{x:.1f}" y="{y:.1f}" fill="{stroke}" font-size="{size}" '
                f'font-family="{_SVG_FONT}" text-anchor="{anchor}">{_esc(body)}</text>'
            )
        return "".join(spans)

    return ""


def to_svg(elements: list[dict[str, Any]]) -> str:
    pad = 36
    min_x, min_y, max_x, max_y = _bounds(elements)
    width = max_x - min_x + pad * 2
    height = max_y - min_y + pad * 2

    colours = sorted(
        {
            element["strokeColor"]
            for element in elements
            if element["type"] == "arrow" and element.get("endArrowhead")
        }
    )
    markers = "".join(
        f'<marker id="a{colour.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
        for colour in colours
    )
    body = "".join(_svg_element(element) for element in elements)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="{min_x - pad:.1f} {min_y - pad:.1f} {width:.1f} {height:.1f}">'
        f"<defs>{markers}</defs>"
        f'<rect x="{min_x - pad:.1f}" y="{min_y - pad:.1f}" width="{width:.1f}" '
        f'height="{height:.1f}" fill="#ffffff"/>'
        f"{body}</svg>\n"
    )


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
    svg = OUT / f"{name}.svg"
    svg.write_text(to_svg(elements), encoding="utf-8")
    print(f"wrote {path.relative_to(OUT.parent.parent)} + {svg.name}")


# --------------------------------------------------------------- 1. system
def system_architecture() -> list[dict[str, Any]]:
    e: list[dict[str, Any]] = []
    e.append(text("Alfresco Curator — deployment", 40, 24, size=24))
    e.append(
        text(
            "Ten services, one compose file. Reads and probes run freely;"
            " every write is approved first.",
            40,
            60,
            size=14,
            colour="#5c5f66",
        )
    )

    # Clients and the model on the left, the platform in one column on the
    # right, so no connector has to cut across a box.
    e += box("Browser", 40, 130, 200, 76, "external")
    e += box("LLM gateway\nOpenAI-compatible", 40, 420, 200, 76, "llm")

    e += box("Share 26.3\n:8180 · page + launcher", 300, 90, 250, 76, "external")
    e += box("Digital Workspace 8.1\n:8280 · launcher injected", 300, 200, 250, 76, "external")
    e += box("Curator API\nFastAPI :8600", 300, 420, 250, 76, "service")

    e += box("Alfresco Repository\nACS 26.3  :8080", 700, 90, 260, 76, "external")
    e += box("ActiveMQ 6.2\nJolokia :8161", 700, 230, 260, 76, "external")
    e += box("Transform core AIO\n:8090", 700, 370, 260, 76, "external")
    e += box("Elasticsearch 8.17\n:9200", 700, 510, 260, 76, "store")
    e += box("Batch indexer 5.8\nactuator :8081", 700, 650, 260, 76, "external")
    e += box("PostgreSQL 17\n:5432", 1060, 90, 220, 76, "store")

    e += arrow(240, 158, 300, 128, "HTTPS", label_at=(232, 96))
    e += arrow(240, 180, 300, 238)

    e += arrow(400, 166, 400, 420, "iframe + ticket", label_at=(338, 280))
    e += arrow(470, 276, 470, 420)

    e += arrow(300, 458, 244, 458)
    e.append(text("translate · explain · rank", 40, 386, size=12, colour=PALETTE["llm"][0]))

    # Each label sits above its own target, which keeps the fan-out readable.
    e += arrow(550, 440, 700, 130)
    e.append(text("REST v1 — search, nodes, ACL", 700, 62, size=12))
    e += arrow(550, 450, 700, 270)
    e.append(text("queue depth", 700, 202, size=12))
    e += arrow(550, 465, 700, 548)
    e.append(text("_doc · _analyze · _count", 700, 482, size=12, colour=PALETTE["store"][0]))
    e += arrow(550, 475, 700, 688)
    e.append(text("/actuator/metrics", 700, 622, size=12))

    e += arrow(960, 128, 1060, 128, "JDBC", label_at=(986, 104))
    e += arrow(830, 166, 830, 230, "content events", label_at=(844, 186))
    e += arrow(830, 306, 830, 370, "transform requests", label_at=(844, 326))
    e += arrow(830, 650, 830, 586, "writes the index", PALETTE["store"][0], label_at=(844, 606))
    e += elbow(960, 690, 1010, 170, "polls the database every 15s", label_at=(974, 664))

    e.append(text("Legend", 40, 528, size=16))
    for index, (style, caption) in enumerate(
        [
            ("service", "our service"),
            ("external", "external system"),
            ("store", "data store"),
            ("llm", "model call"),
        ]
    ):
        e += box(caption, 40, 560 + index * 56, 200, 44, style, size=13)

    e.append(
        text(
            "Indexing polls the database. ActiveMQ carries transform and rendition traffic\n"
            "only — a backlog there starves full-text extraction, but it is NOT on the\n"
            "indexing path, and the report is not allowed to claim otherwise.",
            700,
            770,
            size=13,
            colour="#c92a2a",
        )
    )
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


# --------------------------------------------------- 5. one ask, answered
def ask_sequence() -> list[dict[str, Any]]:
    actors = [
        ("Panel\nbrowser", "external"),
        ("Curator API\nFastAPI", "service"),
        ("Router\nintent.py", "decision"),
        ("Ladder\ngraph.py", "deterministic"),
        ("LLM\ngateway", "llm"),
        ("Alfresco\nSearch API", "external"),
    ]
    messages = [
        (0, 1, "POST /api/agent/ask/stream", "call"),
        (1, 2, "classify(prompt) — lexical, no model", "call"),
        (2, 1, "route = search, writes = false", "return"),
        (1, 0, "event: route — the badge appears before any search runs", "return"),
        (1, 3, "run(prompt)", "call"),
        (3, 4, "translate to AFTS", "model"),
        (4, 3, 'TYPE:"cm:content" AND TEXT:"unpaid"', "return"),
        (3, 5, "POST /search — as the signed-in user", "data"),
        (5, 3, "2 hits", "return"),
        (1, 0, "event: step — plan, search", "return"),
        (3, 4, "write the report from the evidence", "model"),
        (4, 0, "event: token — the report grows a word at a time", "return"),
        (1, 0, "payload + done", "return"),
    ]
    return sequence(
        "One ask, answered",
        "The route is decided before any engine runs, so the panel can say what it is about to do.",
        actors,
        messages,
        "The model is called twice, and both times it is handed evidence that was\n"
        "already gathered. It never decides what to probe, so the same question\n"
        "yields the same investigation.",
    )


# ------------------------------------------- 6. a change, approved and undoable
def act_sequence() -> list[dict[str, Any]]:
    actors = [
        ("Panel\nbrowser", "external"),
        ("Curator API\nFastAPI", "service"),
        ("Router\nintent.py", "decision"),
        ("Deep agent\ndeep.py", "llm"),
        ("Alfresco\nREST v1", "external"),
        ("Audit log\nactions.py", "store"),
    ]
    messages = [
        (0, 1, '"tag every unpaid invoice as overdue"', "call"),
        (1, 2, "classify(prompt)", "call"),
        (2, 1, "route = act, writes = true", "return"),
        (1, 3, "build with write tools + approval gate", "call"),
        (3, 4, "search for unpaid invoices", "data"),
        (4, 3, "2 nodes", "return"),
        (3, 1, "interrupt — proposed add_tag on 2 nodes", "return"),
        (1, 0, "approval card — nothing has changed yet", "return"),
        (0, 1, "approve", "call"),
        (1, 3, "resume", "call"),
        (3, 4, "add_tag", "data"),
        (3, 5, "record the inverse: remove_tag", "call"),
        (1, 0, "done — undoable for the rest of the session", "return"),
    ]
    return sequence(
        "A change, approved and undoable",
        "Writes are refused unless the router asked for them, and every change"
        " records how to put it back.",
        actors,
        messages,
        "The budget is charged before the call, so a refused change costs nothing and a failing\n"
        "tool cannot walk past the limit by retrying.",
    )


if __name__ == "__main__":
    write("01-system-architecture", system_architecture())
    write("02-agent-state-machine", agent_state_machine())
    write("03-diagnostic-layers", diagnostic_layers())
    write("04-search-flow", search_flow())
    write("05-ask-sequence", ask_sequence())
    write("06-act-sequence", act_sequence())
