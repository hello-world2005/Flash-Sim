# -*- coding: utf-8 -*-
"""Flash-Sim 事件泳道图可视化。"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots


REQ_COLORS = {
    "REQ_INIT": "#4E79A7",
    "DELIVER": "#F28E2B",
    "DATA": "#59A14F",
    "REQ_COMP": "#B07AA1",
}

TXN_COLORS = {
    "dispatch": "#76B7B2",
    "CMP_TRANSFERED": "#EDC948",
    "COMPLETE": "#E15759",
    "DATA_TRANSFERED": "#9C755F",
}


def _norm_req_type(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.split(".")[-1]


def _norm_txn_type(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.split(".")[-1]


def _lane_order_for_requests(requests: list[dict[str, Any]]) -> list[int]:
    return sorted({int(r.get("stream_id", 0)) for r in requests})


def _lane_order_for_transactions(transactions: list[dict[str, Any]]) -> list[tuple[int, int]]:
    lanes = {(int(t.get("channel", -1)), int(t.get("chip", -1))) for t in transactions}
    return sorted(lanes, key=lambda x: (x[0], x[1]))


def _add_phase_bars(fig: go.Figure, row: int, items: list[dict[str, Any]], phase_colors: dict[str, str], lane_key_fn, text_fn) -> None:
    phase_map: dict[str, dict[str, list[Any]]] = {}

    for item in items:
        lane_label = lane_key_fn(item)
        segments = item.get("segments", [])
        hover_base = text_fn(item)
        for seg in segments:
            phase = seg.get("phase")
            start = int(seg.get("start", 0))
            end = int(seg.get("end", start + 1))
            width = max(1, end - start)
            bucket = phase_map.setdefault(
                phase,
                {"x": [], "base": [], "y": [], "text": [], "hover": []},
            )
            bucket["x"].append(width)
            bucket["base"].append(start)
            bucket["y"].append(lane_label)
            bucket["text"].append(hover_base)
            bucket["hover"].append(
                f"{hover_base}<br>phase={phase}<br>start={start}<br>end={end}"
            )

    for phase, vals in phase_map.items():
        fig.add_trace(
            go.Bar(
                name=phase,
                x=vals["x"],
                base=vals["base"],
                y=vals["y"],
                orientation="h",
                marker_color=phase_colors.get(phase, "#999999"),
                text=vals["text"],
                textposition="inside",
                hovertext=vals["hover"],
                hoverinfo="text",
                legendgroup=f"row{row}",
            ),
            row=row,
            col=1,
        )


def build_timeline_figure(payload: dict[str, Any]) -> go.Figure:
    requests = payload.get("requests", [])
    transactions = payload.get("transactions", [])

    req_lanes = _lane_order_for_requests(requests)
    txn_lanes = _lane_order_for_transactions(transactions)

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=("Request 流程（按 stream_id）", "Transaction 流程（按 channel × chip）"),
    )

    _add_phase_bars(
        fig,
        row=1,
        items=requests,
        phase_colors=REQ_COLORS,
        lane_key_fn=lambda r: f"stream {int(r.get('stream_id', 0))}",
        text_fn=lambda r: (
            f"{r.get('req_key')} | {_norm_req_type(r.get('type'))} | "
            f"start_lha={r.get('start_lha')} size={r.get('size')}"
        ),
    )

    _add_phase_bars(
        fig,
        row=2,
        items=transactions,
        phase_colors=TXN_COLORS,
        lane_key_fn=lambda t: f"ch{int(t.get('channel', -1))}-chip{int(t.get('chip', -1))}",
        text_fn=lambda t: (
            f"{t.get('txn_key')} | {_norm_txn_type(t.get('type'))} | "
            f"source_req={t.get('source_req')} | "
            f"lpa={t.get('accessed_lpa')} | addr={t.get('accessed_address')}"
        ),
    )

    fig.update_layout(
        barmode="overlay",
        title="Flash-Sim 执行泳道图",
        height=950,
        showlegend=True,
        margin=dict(l=80, r=30, t=70, b=40),
    )

    fig.update_xaxes(title_text="仿真时间", row=2, col=1, rangeslider_visible=True)
    fig.update_yaxes(categoryorder="array", categoryarray=[f"stream {sid}" for sid in req_lanes], row=1, col=1)
    fig.update_yaxes(categoryorder="array", categoryarray=[f"ch{ch}-chip{cp}" for ch, cp in txn_lanes], row=2, col=1)

    return fig


def load_timeline_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def visualize_timeline(
    events_path: str | Path,
    html_output: str | Path = "timeline.html",
    auto_open: bool = True,
) -> Path:
    payload = load_timeline_json(events_path)
    fig = build_timeline_figure(payload)
    html_path = Path(html_output)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(html_path), include_plotlyjs="cdn", auto_open=False)
    if auto_open:
        webbrowser.open(html_path.resolve().as_uri())
    return html_path
