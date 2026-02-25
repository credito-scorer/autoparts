from connectors.sheets import get_order_log

STATUS_STYLES = {
    "received":         ("🔵", "#e3f2fd", "#1565c0"),
    "pending_approval": ("🟡", "#fff3e0", "#e65100"),
    "not_found":        ("🔴", "#fce4ec", "#c62828"),
    "confirmed":        ("🟢", "#e8f5e9", "#2e7d32"),
    "quoted":           ("🟣", "#f3e5f5", "#6a1b9a"),
}

def _badge(status: str) -> str:
    icon, bg, color = STATUS_STYLES.get(status, ("⚪", "#eee", "#333"))
    label = status.replace("_", " ").title()
    return (
        f'<span style="background:{bg};color:{color};padding:3px 10px;'
        f'border-radius:12px;font-size:0.75rem;font-weight:bold;white-space:nowrap">'
        f'{icon} {label}</span>'
    )


def _option_cell(supplier: str, cost: str, lead: str) -> str:
    if not supplier:
        return "—"
    parts = []
    if supplier:
        parts.append(f"<b>{supplier}</b>")
    if cost:
        parts.append(f"${cost}")
    if lead:
        parts.append(f"<span style='color:#888'>{lead}</span>")
    return "<br>".join(parts)


def render_dashboard() -> str:
    try:
        sheet = get_order_log()
        rows = sheet.get_all_values()
    except Exception as e:
        return f"<h2>Error reading sheet: {e}</h2>"

    # Reverse so newest first, skip header row if present
    data = list(reversed(rows))
    if data and data[-1][0].lower() in ("timestamp", "fecha", "date"):
        data = data[:-1]

    # Stats
    statuses = [r[18] if len(r) > 18 else "" for r in data]
    total       = len(data)
    confirmed   = statuses.count("confirmed")
    not_found   = statuses.count("not_found")
    pending     = statuses.count("pending_approval")
    quoted      = statuses.count("quoted")

    def stat_card(label, value, color):
        return (
            f'<div style="background:white;padding:16px 24px;border-radius:10px;'
            f'box-shadow:0 1px 4px rgba(0,0,0,.1);min-width:100px">'
            f'<div style="font-size:2rem;font-weight:bold;color:{color}">{value}</div>'
            f'<div style="color:#666;font-size:0.8rem;margin-top:2px">{label}</div>'
            f'</div>'
        )

    stats_html = "".join([
        stat_card("Total", total, "#333"),
        stat_card("Confirmadas", confirmed, "#2e7d32"),
        stat_card("Cotizadas", quoted, "#6a1b9a"),
        stat_card("No encontradas", not_found, "#c62828"),
        stat_card("Pendientes", pending, "#e65100"),
    ])

    # Table rows
    row_html = ""
    for r in data:
        def g(i): return r[i] if len(r) > i else ""

        timestamp   = g(0)
        raw_msg     = g(1)
        customer    = g(2)
        part        = g(3)
        make        = g(4)
        model       = g(5)
        year        = g(6)
        opt1        = _option_cell(g(7), g(8), g(9))
        opt2        = _option_cell(g(10), g(11), g(12))
        opt3        = _option_cell(g(13), g(14), g(15))
        prices      = g(16)
        chosen      = g(17)
        status      = g(18)

        vehicle = " ".join(filter(None, [make, model, year])) or "—"
        options_html = "<br><br>".join(filter(lambda x: x != "—", [opt1, opt2, opt3])) or "—"
        chosen_html = f"Opción {chosen}" if chosen else "—"

        row_html += f"""
        <tr>
            <td style="white-space:nowrap;color:#888;font-size:0.8rem">{timestamp}</td>
            <td style="white-space:nowrap">{customer}</td>
            <td style="max-width:200px;word-break:break-word;font-style:italic;color:#555">"{raw_msg}"</td>
            <td><b>{part or "—"}</b></td>
            <td>{vehicle}</td>
            <td style="font-size:0.85rem">{options_html}</td>
            <td style="white-space:nowrap">{prices or "—"}<br><span style="color:#888;font-size:0.8rem">{chosen_html}</span></td>
            <td>{_badge(status)}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoParts Dashboard</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #f0f2f5; padding: 24px; color: #333; }}
        h1 {{ font-size: 1.5rem; margin-bottom: 20px; }}
        .stats {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }}
        .table-wrap {{ overflow-x: auto; border-radius: 10px;
                       box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
        table {{ width: 100%; border-collapse: collapse; background: white;
                 font-size: 0.875rem; }}
        th {{ background: #1a1a2e; color: white; padding: 12px 14px;
              text-align: left; font-weight: 600; white-space: nowrap; }}
        td {{ padding: 12px 14px; border-bottom: 1px solid #f0f0f0;
              vertical-align: top; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: #fafafa; }}
        .refresh {{ float: right; font-size: 0.8rem; color: #888; margin-top: 4px; }}
    </style>
</head>
<body>
    <h1>📊 AutoParts Dashboard
        <span class="refresh">Auto-refresh cada 60s</span>
    </h1>
    <div class="stats">{stats_html}</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>Fecha</th>
                    <th>Cliente</th>
                    <th>Mensaje</th>
                    <th>Pieza</th>
                    <th>Vehículo</th>
                    <th>Opciones</th>
                    <th>Precio / Elección</th>
                    <th>Estado</th>
                </tr>
            </thead>
            <tbody>{row_html}</tbody>
        </table>
    </div>
    <meta http-equiv="refresh" content="60">
</body>
</html>"""

    return html
