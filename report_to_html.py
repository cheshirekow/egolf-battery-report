#!/usr/bin/env python3
"""Render a JSON battery report into a self-contained graphical HTML report.

Input is the JSON produced by `get_battery.py --output-format=json` or by
`txt_to_json.py`. Output is a single HTML file with all figures embedded as
base64 PNGs, so it can be opened or emailed without any external assets.

Usage:
    python3 report_to_html.py results-03.json                 # -> results-03.html
    python3 report_to_html.py results-03.json -o report.html

Graphics:
  - SoC shown as a donut gauge.
  - State of Health as a concern bar (red -> green) with a marker at the value.
  - Per-cell voltages as an annotated grid, each cell colored green (in
    balance) through yellow/orange to red (out of balance) by its deviation
    from the pack mean, with the weakest/strongest cells outlined.
  - Cell-module temperatures as a colored bar chart.
  - Pack voltage / current / energy as summary cards.

Requires matplotlib (see requirements.txt).
"""
import argparse
import base64
import datetime
import io
import json
import math
import os
import sys

import matplotlib
matplotlib.use('Agg')  # headless: render to memory, never open a window
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
import matplotlib.cm as cm

# --- Health thresholds (tuned for an MK7 e-Golf 88s pack) -------------------
# Per-cell color is driven by |deviation from the pack mean|, in mV:
#   <= GREEN_MV  fully green ("in balance")
#   >= RED_MV    fully red   ("out of balance")
# with a yellow/orange ramp in between.
CELL_BALANCE_GREEN_MV = 10.0
CELL_BALANCE_RED_MV = 40.0

# Diverging green -> red ramp used for both the cell grid and the legend.
BALANCE_CMAP = LinearSegmentedColormap.from_list(
    'balance',
    ['#1a9850', '#66bd63', '#a6d96a', '#fee08b', '#fc8d59', '#d73027'])

# Concern ramp for the SoH bar: red at 0 %, green at 100 %.
CONCERN_CMAP = LinearSegmentedColormap.from_list(
    'concern',
    ['#d73027', '#fc8d59', '#fee08b', '#91cf60', '#1a9850'])


def _fig_to_b64(fig):
    """Render a Matplotlib figure to a base64 PNG and close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=130, bbox_inches='tight',
                facecolor='white')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _text_on(hex_color):
    """Pick black or white text for legibility over a background color."""
    h = hex_color.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return '#000000' if luminance > 0.6 else '#ffffff'


def _soh_color(pct):
    """Band color for a State-of-Health percentage."""
    if pct >= 90:
        return '#1a9850'
    if pct >= 80:
        return '#66bd63'
    if pct >= 75:
        return '#fee08b'
    if pct >= 70:
        return '#fc8d59'
    return '#d73027'


def build_soc_gauge(soc):
    """Donut gauge for State of Charge."""
    fig, ax = plt.subplots(figsize=(3.2, 3.2))
    ax.pie([soc, 100 - soc],
           colors=['#2c7fb8', '#e6e6e6'],
           startangle=90, counterclock=False,
           wedgeprops=dict(width=0.34, edgecolor='white', linewidth=2))
    ax.text(0, 0, f"{soc:.1f}%\nSoC", ha='center', va='center',
            fontsize=18, fontweight='bold', color='#222')
    ax.set_aspect('equal')
    return _fig_to_b64(fig)


def build_soh_bar(soh):
    """Horizontal concern bar (red -> green) with a marker at the SoH value."""
    fig, ax = plt.subplots(figsize=(8.4, 1.6))
    gradient = [[i / 255.0 for i in range(256)]]
    ax.imshow(gradient, extent=[0, 100, 0, 1], aspect='auto',
              cmap=CONCERN_CMAP, origin='lower')
    ax.axvline(soh, color='#111111', lw=3)
    ax.plot([soh], [1.0], marker='v', markersize=14, color='#111111',
            clip_on=False)
    ax.text(soh, 1.28, f"{soh:.1f}%", ha='center', va='bottom',
            fontsize=15, fontweight='bold')
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(['0', '25', '50', '75', '100%'])
    for spine in ('left', 'right', 'top'):
        ax.spines[spine].set_visible(False)
    ax.tick_params(length=0)
    return _fig_to_b64(fig)


def build_cell_grid(voltages, ncols=8):
    """Annotated per-cell voltage grid colored by imbalance.

    Returns (base64_png, stats_dict) or (None, None) if no cell data.
    """
    valid = [v for v in voltages if v is not None]
    if not valid:
        return None, None
    mean = sum(valid) / len(valid)
    vmin, vmax = min(valid), max(valid)
    min_idx = voltages.index(vmin)
    max_idx = voltages.index(vmax)

    norm = Normalize(CELL_BALANCE_GREEN_MV, CELL_BALANCE_RED_MV)
    nrows = math.ceil(len(voltages) / ncols)

    fig, ax = plt.subplots(figsize=(ncols * 1.18, nrows * 0.82 + 0.9))
    for idx, v in enumerate(voltages):
        row = idx // ncols
        col = idx % ncols
        y = nrows - 1 - row  # fill top-to-bottom
        if v is None:
            face = '#cccccc'
            label = '--'
        else:
            dev_mv = (v - mean) * 1000.0
            face = matplotlib.colors.to_hex(BALANCE_CMAP(norm(abs(dev_mv))))
            label = f"{v:.3f}"
        ax.add_patch(plt.Rectangle((col, y), 0.94, 0.94, facecolor=face,
                                   edgecolor='white', linewidth=1.0))
        # Outline the weakest / strongest cell so they're visible even when
        # the whole pack is green.
        if idx == min_idx:
            ax.add_patch(plt.Rectangle((col, y), 0.94, 0.94, fill=False,
                                       edgecolor='#08306b', linewidth=2.6))
        elif idx == max_idx:
            ax.add_patch(plt.Rectangle((col, y), 0.94, 0.94, fill=False,
                                       edgecolor='#444444', linewidth=2.2))
        tc = _text_on(face)
        ax.text(col + 0.47, y + 0.58, label, ha='center', va='center',
                fontsize=8.5, fontweight='bold', color=tc)
        tag = f"#{idx + 1}"
        if idx == min_idx:
            tag += " ▼"   # down triangle = lowest
        elif idx == max_idx:
            tag += " ▲"   # up triangle = highest
        ax.text(col + 0.47, y + 0.22, tag, ha='center', va='center',
                fontsize=6.2, color=tc)

    ax.set_xlim(0, ncols)
    ax.set_ylim(-0.05, nrows)
    ax.set_aspect('equal')
    ax.axis('off')

    sm = cm.ScalarMappable(cmap=BALANCE_CMAP, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, orientation='horizontal',
                        fraction=0.045, pad=0.03)
    cbar.set_label('|deviation from pack mean| (mV)   '
                   '—   green = in balance, red = out of balance',
                   fontsize=9)

    stats = {
        'mean': mean, 'min': vmin, 'max': vmax,
        'min_cell': min_idx + 1, 'max_cell': max_idx + 1,
        'spread_mv': (vmax - vmin) * 1000.0,
        'count': len(valid),
    }
    return _fig_to_b64(fig), stats


def build_temp_bars(temps):
    """Colored bar chart of module temperatures."""
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    xs = list(range(1, len(temps) + 1))
    norm = Normalize(0, 40)  # 0-40 C spans the realistic pack range
    coolwarm = matplotlib.colormaps['coolwarm']
    colors = [coolwarm(norm(t)) for t in temps]
    ax.bar(xs, temps, color=colors, edgecolor='#333333', linewidth=0.8)
    for x, t in zip(xs, temps):
        ax.text(x, t + 0.4, f"{t:.1f}", ha='center', va='bottom', fontsize=8)
    ax.set_xticks(xs)
    ax.set_xlabel('Module sensor')
    ax.set_ylabel('°C')
    ax.set_ylim(0, max(temps) * 1.25 + 1)
    for spine in ('right', 'top'):
        ax.spines[spine].set_visible(False)
    return _fig_to_b64(fig)


# --- HTML assembly ----------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       margin: 0; background: #f4f6f8; color: #1f2933; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 28px 22px 60px; }
h1 { font-size: 26px; margin: 0 0 2px; }
.sub { color: #66707a; font-size: 13px; margin-bottom: 22px; }
.section { background: #fff; border: 1px solid #e3e8ee; border-radius: 12px;
           padding: 20px 22px; margin-bottom: 20px;
           box-shadow: 0 1px 3px rgba(0,0,0,.04); }
.section h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .04em;
              color: #52606d; margin: 0 0 16px; }
.row { display: flex; flex-wrap: wrap; gap: 18px; align-items: center; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr));
         gap: 14px; }
.card { background: #f8fafc; border: 1px solid #e3e8ee; border-radius: 10px;
        padding: 14px 16px; }
.card .t { font-size: 12px; color: #66707a; text-transform: uppercase;
           letter-spacing: .03em; }
.card .v { font-size: 24px; font-weight: 700; margin-top: 4px; }
.card .s { font-size: 12px; color: #8c97a3; margin-top: 2px; }
.fig { max-width: 100%; height: auto; display: block; }
.center { margin: 0 auto; }
.pill { display: inline-block; padding: 3px 10px; border-radius: 999px;
        font-size: 12px; font-weight: 600; color: #fff; }
.note { color: #8c97a3; font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
footer { color: #8c97a3; font-size: 12px; margin-top: 8px; text-align: center; }
"""


def _fmt(value, suffix='', nd=2, dash='n/a'):
    if value is None:
        return dash
    return f"{value:.{nd}f}{suffix}"


def _card(title, value, sub=''):
    sub_html = f'<div class="s">{sub}</div>' if sub else ''
    return (f'<div class="card"><div class="t">{title}</div>'
            f'<div class="v">{value}</div>{sub_html}</div>')


def _img(b64, alt, cls='fig'):
    return f'<img class="{cls}" src="data:image/png;base64,{b64}" alt="{alt}">'


def build_html(report, source_name):
    soc = report.get('soc_gross_pct')
    soh = report.get('state_of_health_pct')
    voltages = report.get('cell_voltages')
    temps = report.get('module_temps_c')
    balance = report.get('cell_balance') or {}
    nominal = report.get('nominal_pack_kwh')
    cur = report.get('pack_current_a')

    parts = []
    parts.append('<div class="wrap">')
    parts.append('<h1>e-Golf Battery Report</h1>')
    generated = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    parts.append(f'<div class="sub">Source: <span class="mono">{source_name}'
                 f'</span> &middot; generated {generated}</div>')

    # --- State of Charge + Health -------------------------------------------
    parts.append('<div class="section"><h2>Charge &amp; Health</h2>')
    parts.append('<div class="row">')
    if soc is not None:
        parts.append('<div>' + _img(build_soc_gauge(soc), 'State of charge')
                     + '</div>')
    if soh is not None:
        color = _soh_color(soh)
        max_kwh = report.get('max_energy_kwh')
        sub = (f'{_fmt(max_kwh, " kWh")} of {_fmt(nominal, " kWh", 1)} nominal'
               if max_kwh is not None else '')
        parts.append(
            '<div style="flex:1; min-width:340px;">'
            f'<div style="margin-bottom:8px;">State of Health '
            f'<span class="pill" style="background:{color}">'
            f'{soh:.1f}%</span></div>'
            + _img(build_soh_bar(soh), 'State of health bar')
            + (f'<div class="note">{sub}</div>' if sub else '')
            + '</div>')
    parts.append('</div></div>')

    # --- Summary cards ------------------------------------------------------
    if cur is None:
        cur_state = 'n/a'
    elif cur > 0.2:
        cur_state = 'charging'
    elif cur < -0.2:
        cur_state = 'discharging'
    else:
        cur_state = 'at rest'
    parts.append('<div class="section"><h2>Pack summary</h2><div class="cards">')
    parts.append(_card('Pack voltage', _fmt(report.get('pack_voltage_v'), ' V', 1)))
    parts.append(_card('Pack current', _fmt(cur, ' A', 1), cur_state))
    parts.append(_card('Usable energy now',
                       _fmt(report.get('current_usable_energy_kwh'), ' kWh')))
    parts.append(_card('Max energy', _fmt(report.get('max_energy_kwh'), ' kWh')))
    if balance:
        parts.append(_card('Cell spread',
                           _fmt(balance.get('spread_mv'), ' mV', 1),
                           f"min #{balance.get('min_cell')} / "
                           f"max #{balance.get('max_cell')}"))
    parts.append('</div></div>')

    # --- Cell voltages ------------------------------------------------------
    parts.append('<div class="section"><h2>Cell voltages</h2>')
    grid_b64, stats = build_cell_grid(voltages) if voltages else (None, None)
    if grid_b64:
        parts.append(_img(grid_b64, 'Per-cell voltage grid', 'fig center'))
        parts.append(
            f'<div class="note">{stats["count"]} cells &middot; '
            f'mean {stats["mean"]:.3f} V &middot; '
            f'min {stats["min"]:.3f} V (cell #{stats["min_cell"]} ▼) &middot; '
            f'max {stats["max"]:.3f} V (cell #{stats["max_cell"]} ▲) &middot; '
            f'spread {stats["spread_mv"]:.1f} mV</div>')
    else:
        parts.append('<div class="note">No per-cell data in this report. '
                     'Re-run the capture with <span class="mono">--cells</span> '
                     'to include the 88-cell sweep.</div>')
    parts.append('</div>')

    # --- Temperatures -------------------------------------------------------
    if temps:
        parts.append('<div class="section"><h2>Module temperatures</h2>')
        parts.append(_img(build_temp_bars(temps), 'Module temperatures',
                          'fig center'))
        summary = report.get('module_temp_summary') or {}
        if summary:
            parts.append(
                f'<div class="note">min {_fmt(summary.get("min_c"), " °C", 1)}'
                f' &middot; max {_fmt(summary.get("max_c"), " °C", 1)}'
                f' &middot; avg {_fmt(summary.get("avg_c"), " °C", 1)}</div>')
        parts.append('</div>')

    # --- Diagnostic footer --------------------------------------------------
    raw_2ab6 = report.get('gateway_2ab6_raw')
    if raw_2ab6:
        parts.append('<div class="section"><h2>Diagnostic (undecoded)</h2>'
                     f'<div class="note">Gateway DID 2AB6 raw bytes: '
                     f'<span class="mono">{raw_2ab6}</span> '
                     '(layout undetermined)</div></div>')

    parts.append('<footer>e-Golf battery report &middot; values decoded over '
                 'OBD-II / UDS. SoH and gateway scaling are best-effort; see '
                 'project README.</footer>')
    parts.append('</div>')

    body = '\n'.join(parts)
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, '
            f'initial-scale=1"><title>e-Golf Battery Report</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help="path to a JSON report (e.g. results-03.json)")
    ap.add_argument('-o', '--output',
                    help="output HTML path (default: input with .html)")
    args = ap.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        report = json.load(f)

    out_path = args.output
    if not out_path:
        base = args.input[:-5] if args.input.endswith('.json') else args.input
        out_path = base + '.html'

    html = build_html(report, os.path.basename(args.input))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
