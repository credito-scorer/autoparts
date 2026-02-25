from connectors.sheets import get_order_log
from collections import defaultdict

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
        f'<span class="badge" style="background:{bg};color:{color}">'
        f'{icon} {label}</span>'
    )

def _option_cell(supplier, cost, lead):
    if not supplier:
        return "—"
    parts = []
    if supplier: parts.append(f"<b>{supplier}</b>")
    if cost:     parts.append(f"${cost}")
    if lead:     parts.append(f"<span style='color:#888'>{lead}</span>")
    return "<br>".join(parts)

def render_dashboard() -> str:
    try:
        sheet = get_order_log()
        rows = sheet.get_all_values()
    except Exception as e:
        import traceback
        return f"<h2>Error reading sheet: {e}</h2><pre>{traceback.format_exc()}</pre>"

    # Skip header row if present
    data = [r for r in rows if r and r[0].lower() not in ("timestamp", "fecha", "date")]
    data = list(reversed(data))  # newest first

    def g(row, i): return row[i] if len(row) > i else ""

    # Group by customer
    by_customer = defaultdict(list)
    for row in data:
        by_customer[g(row, 2)].append(row)

    # Sort customers by most recent interaction
    customer_list = sorted(by_customer.keys(), key=lambda c: by_customer[c][0][0], reverse=True)

    # Global stats
    statuses = [g(r, 18) for r in data]
    total     = len(data)
    confirmed = statuses.count("confirmed")
    not_found = statuses.count("not_found")
    pending   = statuses.count("pending_approval")
    quoted    = statuses.count("quoted")

    def stat_card(label, value, color):
        return (
            f'<div class="stat-card">'
            f'<div style="font-size:1.8rem;font-weight:700;color:{color}">{value}</div>'
            f'<div style="color:#888;font-size:0.75rem;margin-top:2px">{label}</div>'
            f'</div>'
        )

    stats_html = (
        stat_card("Total", total, "#333") +
        stat_card("Confirmadas", confirmed, "#2e7d32") +
        stat_card("Cotizadas", quoted, "#6a1b9a") +
        stat_card("No encontradas", not_found, "#c62828") +
        stat_card("Pendientes", pending, "#e65100")
    )

    # Sidebar customer items
    sidebar_items = ""
    for customer in customer_list:
        rows_c    = by_customer[customer]
        count     = len(rows_c)
        last_status = g(rows_c[0], 18)
        last_date   = g(rows_c[0], 0)[:10]
        icon        = STATUS_STYLES.get(last_status, ("⚪",))[0]
        label       = f"{count} msg · {icon} {last_date}"
        sidebar_items += (
            f'<div class="customer-item" data-customer="{customer}" '
            f'onclick="filterCustomer(this,\'{customer}\')">'
            f'<div class="cnum">{customer}</div>'
            f'<div class="cmeta">{label}</div>'
            f'</div>'
        )

    # Table rows
    row_html = ""
    for row in data:
        customer = g(row, 2)
        opt1 = _option_cell(g(row, 7),  g(row, 8),  g(row, 9))
        opt2 = _option_cell(g(row, 10), g(row, 11), g(row, 12))
        opt3 = _option_cell(g(row, 13), g(row, 14), g(row, 15))
        options_html = "<br><br>".join(x for x in [opt1, opt2, opt3] if x != "—") or "—"
        vehicle  = " ".join(filter(None, [g(row,4), g(row,5), g(row,6)])) or "—"
        prices   = g(row, 16) or "—"
        chosen   = f"Opción {g(row,17)}" if g(row,17) else "—"
        status   = g(row, 18)
        row_html += (
            f'<tr data-customer="{customer}">'
            f'<td style="white-space:nowrap;color:#888;font-size:0.8rem">{g(row,0)}</td>'
            f'<td style="max-width:220px;word-break:break-word;font-style:italic;color:#555">"{g(row,1)}"</td>'
            f'<td><b>{g(row,3) or "—"}</b></td>'
            f'<td>{vehicle}</td>'
            f'<td style="font-size:0.82rem">{options_html}</td>'
            f'<td style="white-space:nowrap">{prices}<br><span style="color:#888;font-size:0.78rem">{chosen}</span></td>'
            f'<td>{_badge(status)}</td>'
            f'</tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoParts Dashboard</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #f0f2f5; color: #333; height: 100vh; display: flex; flex-direction: column; }}

        /* Top bar */
        .topbar {{ background: #1a1a2e; color: white; padding: 14px 24px;
                   display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }}
        .topbar h1 {{ font-size: 1.1rem; }}
        .topbar small {{ color: #aaa; font-size: 0.78rem; }}

        /* Stats */
        .stats {{ display: flex; gap: 10px; padding: 14px 24px; flex-shrink: 0; flex-wrap: wrap; }}
        .stat-card {{ background: white; padding: 12px 20px; border-radius: 8px;
                      box-shadow: 0 1px 3px rgba(0,0,0,.08); min-width: 90px; }}

        /* Body layout */
        .body {{ display: flex; flex: 1; overflow: hidden; gap: 0; }}

        /* Sidebar */
        .sidebar {{ width: 240px; background: white; border-right: 1px solid #e8e8e8;
                    display: flex; flex-direction: column; flex-shrink: 0; }}
        .sidebar-header {{ padding: 12px; border-bottom: 1px solid #eee; }}
        .sidebar-header input {{
            width: 100%; padding: 7px 10px; border: 1px solid #ddd;
            border-radius: 6px; font-size: 0.85rem; outline: none;
        }}
        .all-btn {{ display: block; width: 100%; text-align: left; padding: 10px 14px;
                    background: none; border: none; border-bottom: 1px solid #eee;
                    font-size: 0.85rem; cursor: pointer; color: #555; font-weight: 600; }}
        .all-btn:hover, .all-btn.active {{ background: #f0f2f5; color: #1a1a2e; }}
        .customer-list {{ overflow-y: auto; flex: 1; }}
        .customer-item {{ padding: 10px 14px; border-bottom: 1px solid #f5f5f5;
                          cursor: pointer; transition: background .15s; }}
        .customer-item:hover {{ background: #f7f7f7; }}
        .customer-item.active {{ background: #e8f0fe; border-left: 3px solid #1a1a2e; }}
        .cnum {{ font-size: 0.85rem; font-weight: 600; color: #222; }}
        .cmeta {{ font-size: 0.75rem; color: #888; margin-top: 2px; }}

        /* Table area */
        .main {{ flex: 1; overflow: auto; padding: 16px; }}
        .table-wrap {{ background: white; border-radius: 10px;
                       box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }}
        .table-title {{ padding: 12px 16px; font-weight: 600; font-size: 0.9rem;
                        border-bottom: 1px solid #f0f0f0; color: #555; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
        th {{ background: #1a1a2e; color: white; padding: 10px 12px;
              text-align: left; white-space: nowrap; font-weight: 600; }}
        td {{ padding: 10px 12px; border-bottom: 1px solid #f5f5f5; vertical-align: top; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: #fafafa; }}
        .badge {{ padding: 3px 9px; border-radius: 10px; font-size: 0.73rem;
                  font-weight: 700; white-space: nowrap; }}
        .empty {{ text-align: center; padding: 40px; color: #aaa; font-size: 0.9rem; }}
    </style>
</head>
<body>

<div class="topbar">
    <h1>📊 AutoParts Dashboard</h1>
    <small>Auto-refresh cada 60s</small>
</div>

<div class="stats">{stats_html}</div>

<div class="body">
    <div class="sidebar">
        <div class="sidebar-header">
            <input type="text" id="search" placeholder="Buscar cliente..." oninput="searchClients(this.value)">
        </div>
        <button class="all-btn active" id="all-btn" onclick="showAll()">
            Todos los clientes ({len(customer_list)})
        </button>
        <div class="customer-list" id="customer-list">
            {sidebar_items}
        </div>
    </div>

    <div class="main">
        <div class="table-wrap">
            <div class="table-title" id="table-title">Todas las interacciones ({total})</div>
            <table>
                <thead>
                    <tr>
                        <th>Fecha</th>
                        <th>Mensaje</th>
                        <th>Pieza</th>
                        <th>Vehículo</th>
                        <th>Opciones</th>
                        <th>Precio / Elección</th>
                        <th>Estado</th>
                    </tr>
                </thead>
                <tbody id="tbody">{row_html}</tbody>
            </table>
            <div class="empty" id="empty" style="display:none">Sin resultados</div>
        </div>
    </div>
</div>

<script>
    let active = null;

    function filterCustomer(el, customer) {{
        if (active === customer) {{ showAll(); return; }}
        active = customer;

        document.querySelectorAll('.customer-item').forEach(i => i.classList.remove('active'));
        document.getElementById('all-btn').classList.remove('active');
        el.classList.add('active');

        const rows = document.querySelectorAll('#tbody tr');
        let count = 0;
        rows.forEach(r => {{
            const show = r.dataset.customer === customer;
            r.style.display = show ? '' : 'none';
            if (show) count++;
        }});

        document.getElementById('table-title').textContent =
            customer + ' — ' + count + ' interacción' + (count !== 1 ? 'es' : '');
        document.getElementById('empty').style.display = count === 0 ? '' : 'none';
    }}

    function showAll() {{
        active = null;
        document.querySelectorAll('.customer-item').forEach(i => i.classList.remove('active'));
        document.getElementById('all-btn').classList.add('active');
        document.querySelectorAll('#tbody tr').forEach(r => r.style.display = '');
        document.getElementById('table-title').textContent = 'Todas las interacciones ({total})';
        document.getElementById('empty').style.display = 'none';
    }}

    function searchClients(q) {{
        document.querySelectorAll('.customer-item').forEach(item => {{
            item.style.display = item.dataset.customer.includes(q) ? '' : 'none';
        }});
    }}
</script>

<meta http-equiv="refresh" content="60">
</body>
</html>"""
