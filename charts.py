"""
Chart rendering for the run analysis page — matplotlib, object-oriented API
(no pyplot global state, safe under concurrent requests).

Two themes: 'dark' matches the app UI (embedded <img>), 'light' is
publication-ready (white background) and is what the download buttons serve.
Formats: png (dpi 150) and pdf (vector).
"""
import io

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_pdf import FigureCanvasPdf
from matplotlib.figure import Figure

THEMES = {
    "dark": {"bg": "#0f1117", "panel": "#1a1d27", "text": "#e2e8f0",
             "muted": "#a0aec0", "grid": "#2d3148", "bar": "#4a6fa5",
             "accent": "#63b3ed", "cmap": "mako"},
    "light": {"bg": "#ffffff", "panel": "#ffffff", "text": "#1c2430",
              "muted": "#4a5568", "grid": "#d9dee5", "bar": "#2f5d9e",
              "accent": "#1d4ed8", "cmap": "viridis"},
}

GROUP_COLORS = ["#4a6fa5", "#b7c842", "#b794f4", "#f6ad55", "#68d391", "#fc8181"]


def _fig(theme: str, height: float, width: float = 9.0) -> tuple[Figure, dict]:
    t = THEMES[theme]
    fig = Figure(figsize=(width, max(2.4, height)), facecolor=t["bg"])
    return fig, t


def _style_axes(ax, t):
    ax.set_facecolor(t["bg"])
    for spine in ax.spines.values():
        spine.set_color(t["grid"])
    ax.tick_params(colors=t["muted"], labelsize=9)
    ax.xaxis.label.set_color(t["muted"])
    ax.yaxis.label.set_color(t["muted"])
    ax.title.set_color(t["text"])


def _to_bytes(fig: Figure, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "pdf":
        FigureCanvasPdf(fig)
        fig.savefig(buf, format="pdf", facecolor=fig.get_facecolor(), bbox_inches="tight")
    else:
        FigureCanvasAgg(fig)
        fig.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor(),
                    bbox_inches="tight")
    return buf.getvalue()


def _barh(items: list[tuple[str, float]], title: str, xlabel: str,
          theme: str, fmt: str) -> bytes:
    items = items[::-1]  # largest on top
    fig, t = _fig(theme, 0.42 * len(items) + 1.2)
    ax = fig.add_subplot(111)
    labels = [i[0] for i in items]
    values = [i[1] for i in items]
    ax.barh(range(len(items)), values, color=t["bar"], height=0.62)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(labels)
    ax.set_xlabel(xlabel)
    # loc="left" titles live in ax._left_title, which _style_axes cannot reach:
    # the color must be set here at creation
    ax.set_title(title, loc="left", fontsize=11, color=t["text"])
    ax.grid(axis="x", color=t["grid"], linewidth=0.5, alpha=0.6)
    ax.set_axisbelow(True)
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:g}", va="center", color=t["muted"], fontsize=8)
    _style_axes(ax, t)
    return _to_bytes(fig, fmt)


def _matrix(labels: list[str], matrix: list[list], title: str,
            theme: str, fmt: str) -> bytes:
    n = len(labels)
    fig, t = _fig(theme, 0.5 * n + 1.6, width=max(6.0, 0.55 * n + 3))
    ax = fig.add_subplot(111)
    im = ax.imshow(matrix, cmap="viridis" if theme == "light" else "cividis")
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_yticks(range(n))
    ax.set_yticklabels(labels)
    vmax = max((max(row) for row in matrix), default=0)
    for i in range(n):
        for j in range(n):
            v = matrix[i][j]
            if v:
                ax.text(j, i, str(v), ha="center", va="center", fontsize=8,
                        color="#ffffff" if v < 0.6 * max(vmax, 1) else "#111111")
    ax.set_title(title, loc="left", fontsize=11, color=t["text"])
    cbar = fig.colorbar(im, ax=ax, fraction=0.04)
    cbar.ax.tick_params(colors=t["muted"], labelsize=8)
    cbar.outline.set_edgecolor(t["grid"])
    _style_axes(ax, t)
    return _to_bytes(fig, fmt)


def render_chart(name: str, data: dict, theme: str = "dark", fmt: str = "png",
                 code: str | None = None, group: str | None = None) -> bytes | None:
    """Render one analysis chart. Returns None when the block has no data.
    For lemma charts, `code` (and optionally `group`) select the drill-down cell."""
    if name == "codes":
        items = [(b["label"], b["codings"]) for b in data["codes"]]
        return _barh(items, "Codings per code", "codings", theme, fmt) if items else None

    if name == "groups":
        g = data.get("groups")
        if not g:
            return None
        groups = g["groups"]
        rows = g["rows"]
        fig, t = _fig(theme, 0.5 * len(rows) + 1.6)
        ax = fig.add_subplot(111)
        bar_h = 0.8 / max(1, len(groups))
        for k, grp in enumerate(groups):
            ys = [i + k * bar_h for i in range(len(rows))]
            ax.barh(ys, [r["pct"][grp] for r in rows], height=bar_h * 0.92,
                    color=GROUP_COLORS[k % len(GROUP_COLORS)], label=grp or "(no group)")
        ax.set_yticks([i + 0.4 for i in range(len(rows))])
        ax.set_yticklabels([r["label"] for r in rows])
        ax.invert_yaxis()
        ax.set_xlabel("% of group's units coded")
        ax.set_title("Codes by group (normalized)", loc="left", fontsize=11, color=t["text"])
        leg = ax.legend(fontsize=8, facecolor=t["panel"], edgecolor=t["grid"],
                        labelcolor=t["text"])
        ax.grid(axis="x", color=t["grid"], linewidth=0.5, alpha=0.6)
        ax.set_axisbelow(True)
        _style_axes(ax, t)
        return _to_bytes(fig, fmt)

    if name == "cooccurrence":
        c = data["cooccurrence"]
        if not c["labels"]:
            return None
        return _matrix(c["labels"], c["matrix"], "Code co-occurrence (units)", theme, fmt)

    if name == "documents":
        d = data["documents"]
        if not d["rows"] or not d["codes"]:
            return None
        labels_y = [f'{r["document"]} ({r["coverage_pct"]}%)' for r in d["rows"]]
        matrix = [r["counts"] for r in d["rows"]]
        n_x, n_y = len(d["codes"]), len(labels_y)
        fig, t = _fig(theme, 0.45 * n_y + 1.8, width=max(6.0, 0.55 * n_x + 4))
        ax = fig.add_subplot(111)
        im = ax.imshow(matrix, cmap="viridis" if theme == "light" else "cividis",
                       aspect="auto")
        ax.set_xticks(range(n_x))
        ax.set_xticklabels(d["codes"], rotation=45, ha="right")
        ax.set_yticks(range(n_y))
        ax.set_yticklabels(labels_y)
        vmax = max((max(row) for row in matrix), default=0)
        for i in range(n_y):
            for j in range(n_x):
                v = matrix[i][j]
                if v:
                    ax.text(j, i, str(v), ha="center", va="center", fontsize=8,
                            color="#ffffff" if v < 0.6 * max(vmax, 1) else "#111111")
        ax.set_title("Documents × codes (units; coverage in label)", loc="left", fontsize=11,
                     color=t["text"])
        _style_axes(ax, t)
        return _to_bytes(fig, fmt)

    if name.startswith("lemmas_"):
        lang = name.split("_", 1)[1]
        if code and group:
            lookup = "" if group == "(no group)" else group
            cells = [c for c in data.get("lemmas_by_code_group", {}).get(lang, [])
                     if c["code"] == code and c["group"] == lookup]
            block = cells[0] if cells else None
            title = f"Top lemmas — {code} / {group} ({lang})"
        elif code:
            cells = [c for c in data.get("lemmas_by_code", {}).get(lang, [])
                     if c["code"] == code]
            block = cells[0] if cells else None
            title = f"Top lemmas — {code} ({lang})"
        else:
            block = data["lemmas"].get(lang)
            title = f"Top lemmas in coded segments ({lang})"
        if not block or not block["top"]:
            return None
        if block.get("flagged"):
            title += f"  [low volume: {block['total']} lemmas]"
        items = [(x["lemma"], x["freq"]) for x in block["top"][:20]]
        return _barh(items, title, "frequency", theme, fmt)

    if name.startswith("expressions_"):
        lang = name.split("_", 1)[1]
        if code and group:
            lookup = "" if group == "(no group)" else group
            src = [x for x in data.get("expressions_by_code_group", {}).get(lang, [])
                   if x["code"] == code and x["group"] == lookup]
            label = lambda x: x["expression"]
            title = f"Expression firings — {code} / {group} ({lang})"
        elif code:
            src = [x for x in data.get("expressions", {}).get(lang, []) if x["code"] == code]
            label = lambda x: x["expression"]
            title = f"Expression firings — {code} ({lang})"
        else:
            src = data.get("expressions", {}).get(lang, [])
            label = lambda x: f'{x["code"]}: {x["expression"]}'
            title = f"Expression firings ({lang})"
        if not src:
            return None
        items = [(label(x), x["firings"]) for x in src[:25]]
        return _barh(items, title, "firings", theme, fmt) if items else None

    return None


def available_charts(data: dict) -> list[str]:
    names = ["codes"]
    if data.get("groups"):
        names.append("groups")
    if data["cooccurrence"]["labels"]:
        names += ["cooccurrence", "documents"]
    names += [f"lemmas_{lang}" for lang in sorted(data.get("lemmas", {}))]
    exprs = data.get("expressions")
    if isinstance(exprs, dict):  # new schema: {lang: [...]}; old caches were a flat list
        names += [f"expressions_{lang}" for lang in sorted(exprs)]
    return names
