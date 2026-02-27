from connectors.sheets import get_order_log
from collections import defaultdict
from datetime import datetime, timedelta
import statistics
import json

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


def _g(row, i):
    return row[i] if len(row) > i else ""


def _parse_dt(ts: str):
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[:19], fmt)
        except Exception:
            pass
    return None


def _compute_metrics(data):
    now = datetime.now()
    order_groups = defaultdict(list)
    for row in data:
        key = (_g(row, 2), _g(row, 3) or "__x__", _g(row, 4), _g(row, 5), _g(row, 6))
        order_groups[key].append(row)

    quote_times, turns_list, delivery_times = [], [], []
    for rows_g in order_groups.values():
        turns_list.append(len(rows_g))
        srt = sorted(rows_g, key=lambda r: _g(r, 0))
        first_dt = _parse_dt(_g(srt[0], 0))
        for r in srt:
            if _g(r, 18) in ("quoted", "confirmed"):
                qdt = _parse_dt(_g(r, 0))
                if first_dt and qdt:
                    dm = (qdt - first_dt).total_seconds() / 60
                    if 0 <= dm < 1440:
                        quote_times.append(dm)
                break
        for r in srt:
            if _g(r, 19):
                ddt = _parse_dt(_g(r, 19))
                if first_dt and ddt:
                    dh = (ddt - first_dt).total_seconds() / 3600
                    if 0 <= dh < 720:
                        delivery_times.append(dh)
                break

    statuses = [_g(r, 18) for r in data]
    conf = statuses.count("confirmed")
    quot = statuses.count("quoted")
    pend = statuses.count("pending_approval")
    conv_d = conf + quot + pend
    conversion = f"{conf / conv_d * 100:.0f}%" if conv_d >= 5 else "—"

    with_part = [r for r in data if _g(r, 3)]
    src_d = len(with_part)
    src_ok = sum(1 for r in with_part if _g(r, 18) in ("quoted", "confirmed"))
    sourcing = f"{src_ok / src_d * 100:.0f}%" if src_d >= 5 else "—"

    week_ago = now - timedelta(days=7)
    makes = [_g(r, 4) for r in data
             if _parse_dt(_g(r, 0)) and _parse_dt(_g(r, 0)) >= week_ago and _g(r, 4)]
    top_make = max(set(makes), key=makes.count) if makes else "—"

    if delivery_times:
        ad = statistics.mean(delivery_times)
        avg_del = f"{ad:.0f}h" if ad < 24 else f"{ad / 24:.1f}d"
    else:
        avg_del = "—"

    return {
        "avg_quote_time": f"{statistics.mean(quote_times):.0f} min" if len(quote_times) >= 3 else "—",
        "avg_turns":      f"{statistics.mean(turns_list):.1f}"      if len(turns_list) >= 3  else "—",
        "conversion":     conversion,
        "sourcing":       sourcing,
        "top_make":       top_make,
        "avg_delivery":   avg_del,
    }


def _compute_funnel(data):
    groups = defaultdict(list)
    for row in data:
        c = _g(row, 2)
        p = _g(row, 3)
        key = (c, p) if p else (c, f"__ts_{_g(row, 0)}")
        groups[key].append(row)

    n_init = len(groups)
    n_id   = sum(1 for k in groups if k[1] and not k[1].startswith("__ts_"))
    n_quot = sum(1 for gr in groups.values()
                 if any(_g(r, 18) in ("quoted", "confirmed") for r in gr))
    n_conf = sum(1 for gr in groups.values()
                 if any(_g(r, 18) == "confirmed" for r in gr))
    n_del  = sum(1 for gr in groups.values() if any(_g(r, 19) for r in gr))

    def drop(curr, prev):
        if not prev or curr >= prev:
            return ""
        return f"▼ {(prev - curr) / prev * 100:.0f}%"

    return [
        ("Conversación iniciada", n_init, ""),
        ("Pieza identificada",    n_id,   drop(n_id,   n_init)),
        ("Cotización enviada",    n_quot, drop(n_quot, n_id)),
        ("Confirmado",            n_conf, drop(n_conf, n_quot)),
        ("Entregado",             n_del,  drop(n_del,  n_conf)),
    ]


def _compute_sourcing(data, cutoff_dt=None):
    rows = [r for r in data
            if _g(r, 3) and
            (not cutoff_dt or (_parse_dt(_g(r, 0)) and _parse_dt(_g(r, 0)) >= cutoff_dt))]
    by_part = defaultdict(list)
    for r in rows:
        by_part[_g(r, 3).strip()].append(r)

    top_parts, gaps = [], []
    for pname, pr in sorted(by_part.items(), key=lambda x: -len(x[1])):
        total  = len(pr)
        ok     = sum(1 for r in pr if _g(r, 18) in ("quoted", "confirmed"))
        pct    = ok / total * 100 if total else 0
        if len(top_parts) < 10:
            top_parts.append([pname, total, f"{pct:.0f}%"])
        if pct == 0 and len(gaps) < 10:
            makes = [_g(r, 4) for r in pr if _g(r, 4)]
            tm = max(set(makes), key=makes.count) if makes else "—"
            gaps.append([pname, tm, total])

    return top_parts, gaps


def render_dashboard() -> str:
    try:
        sheet = get_order_log()
        rows  = sheet.get_all_values()
    except Exception as e:
        import traceback
        return f"<h2>Error reading sheet: {e}</h2><pre>{traceback.format_exc()}</pre>"

    data = [r for r in rows if r and r[0].lower() not in ("timestamp", "fecha", "date")]
    data = list(reversed(data))

    # ── Existing: group by customer ──
    by_customer = defaultdict(list)
    for row in data:
        by_customer[_g(row, 2)].append(row)
    customer_list = sorted(by_customer.keys(),
                           key=lambda c: by_customer[c][0][0], reverse=True)

    # ── Existing stats ──
    statuses  = [_g(r, 18) for r in data]
    total     = len(data)
    confirmed = statuses.count("confirmed")
    not_found = statuses.count("not_found")
    pending   = statuses.count("pending_approval")
    quoted    = statuses.count("quoted")

    def stat_card(label, value, color, extra_cls=""):
        cls = f"stat-card {extra_cls}".strip()
        return (
            f'<div class="{cls}">'
            f'<div style="font-size:1.8rem;font-weight:700;color:{color}">{value}</div>'
            f'<div style="color:#888;font-size:0.75rem;margin-top:2px">{label}</div>'
            f'</div>'
        )

    stats_html = (
        stat_card("Total",          total,     "#333") +
        stat_card("Confirmadas",    confirmed, "#2e7d32") +
        stat_card("Cotizadas",      quoted,    "#6a1b9a") +
        stat_card("No encontradas", not_found, "#c62828") +
        stat_card("Pendientes",     pending,   "#e65100")
    )

    # ── Section 1: Performance metrics ──
    m = _compute_metrics(data)
    perf_html = (
        stat_card("⏱️ Tiempo a cotización", m["avg_quote_time"], "#1565c0", "perf-card") +
        stat_card("🔄 Turnos promedio",      m["avg_turns"],      "#e65100", "perf-card") +
        stat_card("✅ Tasa de conversión",   m["conversion"],     "#2e7d32", "perf-card") +
        stat_card("🔍 Éxito de sourcing",    m["sourcing"],       "#6a1b9a", "perf-card") +
        stat_card("🚗 Make más solicitado",  m["top_make"],       "#1a1a2e", "perf-card") +
        stat_card("🚚 Tiempo de entrega",    m["avg_delivery"],   "#c62828", "perf-card")
    )

    # ── Sidebar customer items (unchanged) ──
    sidebar_items = ""
    for customer in customer_list:
        rows_c      = by_customer[customer]
        count       = len(rows_c)
        last_status = _g(rows_c[0], 18)
        last_date   = _g(rows_c[0], 0)[:10]
        icon        = STATUS_STYLES.get(last_status, ("⚪",))[0]
        label       = f"{count} msg · {icon} {last_date}"
        sidebar_items += (
            f'<div class="customer-item" data-customer="{customer}" '
            f'onclick="filterCustomer(this,\'{customer}\')">'
            f'<div class="cnum">{customer}</div>'
            f'<div class="cmeta">{label}</div>'
            f'</div>'
        )

    # ── Table rows (+ delivery column) ──
    row_html = ""
    for row in data:
        customer     = _g(row, 2)
        opt1         = _option_cell(_g(row, 7),  _g(row, 8),  _g(row, 9))
        opt2         = _option_cell(_g(row, 10), _g(row, 11), _g(row, 12))
        opt3         = _option_cell(_g(row, 13), _g(row, 14), _g(row, 15))
        options_html = "<br><br>".join(x for x in [opt1, opt2, opt3] if x != "—") or "—"
        vehicle      = " ".join(filter(None, [_g(row, 4), _g(row, 5), _g(row, 6)])) or "—"
        prices       = _g(row, 16) or "—"
        chosen       = f"Opción {_g(row, 17)}" if _g(row, 17) else "—"
        status       = _g(row, 18)
        del_ts       = _g(row, 19)
        row_ts       = _g(row, 0)

        if del_ts:
            del_cell = f'<span class="delivered-tag">✅ Entregado<br><small>{del_ts[:16]}</small></span>'
        elif status == "confirmed":
            safe_ts   = row_ts.replace("'", "\\'")
            safe_cust = customer.replace("'", "\\'")
            del_cell  = (
                f'<button class="deliver-btn" '
                f'onclick="markDelivered(\'{safe_ts}\',\'{safe_cust}\',this)">'
                f'✓ Marcar entregado</button>'
            )
        else:
            del_cell = "—"

        row_html += (
            f'<tr data-customer="{customer}">'
            f'<td style="white-space:nowrap;color:#888;font-size:0.8rem">{_g(row,0)}</td>'
            f'<td style="white-space:nowrap;font-weight:600;font-size:0.82rem">{customer}</td>'
            f'<td style="max-width:220px;word-break:break-word;font-style:italic;color:#555">"{_g(row,1)}"</td>'
            f'<td><b>{_g(row,3) or "—"}</b></td>'
            f'<td>{vehicle}</td>'
            f'<td style="font-size:0.82rem">{options_html}</td>'
            f'<td style="white-space:nowrap">{prices}<br>'
            f'<span style="color:#888;font-size:0.78rem">{chosen}</span></td>'
            f'<td>{_badge(status)}</td>'
            f'<td>{del_cell}</td>'
            f'</tr>'
        )

    # ── Section 3: Funnel ──
    funnel_stages = _compute_funnel(data)
    funnel_html = ""
    for i, (label, count_f, drop) in enumerate(funnel_stages):
        drop_html = f'<span class="funnel-drop">{drop}</span>' if drop else ""
        funnel_html += (
            f'<div class="funnel-stage">'
            f'<div class="funnel-count">{count_f}</div>'
            f'<div class="funnel-label">{label}</div>'
            f'{drop_html}'
            f'</div>'
        )
        if i < len(funnel_stages) - 1:
            funnel_html += '<div class="funnel-arrow">→</div>'

    # ── Section 2: Sourcing intelligence data ──
    now = datetime.now()
    top_week,  gaps_week  = _compute_sourcing(data, now - timedelta(days=7))
    top_month, gaps_month = _compute_sourcing(data, now - timedelta(days=30))
    top_all,   gaps_all   = _compute_sourcing(data)
    sourcing_json = json.dumps({
        "week":  {"top": top_week,  "gaps": gaps_week},
        "month": {"top": top_month, "gaps": gaps_month},
        "all":   {"top": top_all,   "gaps": gaps_all},
    }, ensure_ascii=False)

    # ══════════════════════════════════════════════════════
    # CSS — regular string (no f-string, braces are literal)
    # ══════════════════════════════════════════════════════
    CSS = """
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f0f2f5; color: #333;
            min-height: 100vh; display: flex; flex-direction: column;
        }

        /* ── Top bar ── */
        .topbar {
            background: #1a1a2e; color: white; padding: 14px 24px;
            display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
        }
        .topbar h1 { font-size: 1.1rem; }
        .topbar small { color: #aaa; font-size: 0.78rem; }

        /* ── Stat rows ── */
        .stats        { display: flex; gap: 10px; padding: 14px 24px 0; flex-wrap: wrap; flex-shrink: 0; }
        .perf-metrics { display: flex; gap: 10px; padding: 10px 24px 14px; flex-wrap: wrap; flex-shrink: 0; }
        .stat-card, .perf-card {
            background: white; padding: 12px 20px; border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); flex: 1; min-width: 80px;
        }

        /* ── Body (sidebar + table) ── */
        .body { display: flex; flex: 1; gap: 0; }
        .sidebar {
            width: 240px; background: white; border-right: 1px solid #e8e8e8;
            display: flex; flex-direction: column; flex-shrink: 0;
        }
        .sidebar-header { padding: 12px; border-bottom: 1px solid #eee; }
        .sidebar-header input {
            width: 100%; padding: 7px 10px; border: 1px solid #ddd;
            border-radius: 6px; font-size: 0.85rem; outline: none;
        }
        .all-btn {
            display: block; width: 100%; text-align: left; padding: 10px 14px;
            background: none; border: none; border-bottom: 1px solid #eee;
            font-size: 0.85rem; cursor: pointer; color: #555; font-weight: 600; min-height: 44px;
        }
        .all-btn:hover, .all-btn.active { background: #f0f2f5; color: #1a1a2e; }
        .customer-list { overflow-y: auto; flex: 1; }
        .customer-item {
            padding: 10px 14px; border-bottom: 1px solid #f5f5f5;
            cursor: pointer; transition: background .15s;
        }
        .customer-item:hover { background: #f7f7f7; }
        .customer-item.active { background: #e8f0fe; border-left: 3px solid #1a1a2e; }
        .cnum { font-size: 0.85rem; font-weight: 600; color: #222; }
        .cmeta { font-size: 0.75rem; color: #888; margin-top: 2px; }

        /* ── Main table area ── */
        .main { flex: 1; overflow: auto; padding: 16px; }
        .table-wrap {
            background: white; border-radius: 10px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden;
        }
        .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
        .table-title {
            padding: 12px 16px; font-weight: 600; font-size: 0.9rem;
            border-bottom: 1px solid #f0f0f0; color: #555;
        }
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
        th {
            background: #1a1a2e; color: white; padding: 10px 12px;
            text-align: left; white-space: nowrap; font-weight: 600;
        }
        td { padding: 10px 12px; border-bottom: 1px solid #f5f5f5; vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: #fafafa; }
        .badge {
            padding: 3px 9px; border-radius: 10px; font-size: 0.73rem;
            font-weight: 700; white-space: nowrap;
        }
        .empty { text-align: center; padding: 40px; color: #aaa; font-size: 0.9rem; }

        /* ── Collapsible sections ── */
        .sec-wrapper { background: white; border-radius: 8px;
                       box-shadow: 0 1px 4px rgba(0,0,0,.08);
                       margin: 14px 16px 0; overflow: hidden; }
        .sec-toggle {
            display: flex; align-items: center; justify-content: space-between;
            cursor: pointer; user-select: none;
            padding: 13px 16px; font-weight: 600; font-size: 0.92rem;
            color: #1a1a2e; transition: background .15s;
        }
        .sec-toggle:hover { background: #f8f9ff; }
        .sec-toggle .sec-title { display: flex; align-items: center; gap: 8px; }
        .sec-toggle .sec-count { font-size: 0.75rem; color: #888;
                                  font-weight: 400; margin-left: 4px; }
        .chevron { font-size: 0.8rem; color: #aaa; transition: transform .25s; flex-shrink: 0; }
        .sec-body { border-top: 1px solid #f0f0f0; }
        .sec-body.collapsed { display: none; }

        /* Table self-scrolls vertically — no page scroll needed for long lists */
        .table-scroll { overflow-x: auto; overflow-y: auto;
                        max-height: 520px; -webkit-overflow-scrolling: touch; }

        /* ── Delivery ── */
        .deliver-btn {
            padding: 7px 12px; background: #1a1a2e; color: white; border: none;
            border-radius: 5px; cursor: pointer; font-size: 0.75rem; white-space: nowrap;
            min-height: 36px; transition: background .15s;
        }
        .deliver-btn:hover { background: #2d2d4e; }
        .deliver-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .delivered-tag { color: #2e7d32; font-size: 0.78rem; font-weight: 600; line-height: 1.5; }
        .delivered-tag small { color: #888; font-weight: 400; display: block; }

        /* ── Below-body sections ── */
        .sections { padding: 0 16px 32px; display: flex; flex-direction: column; gap: 16px; }
        .section-card {
            background: white; border-radius: 10px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; padding: 0;
        }
        .section-sub { font-size: 0.78rem; color: #888; margin-bottom: 14px; }

        /* Period toggle */
        .period-toggle { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
        .period-btn {
            padding: 7px 18px; border: 1px solid #ddd; background: white;
            border-radius: 20px; cursor: pointer; font-size: 0.82rem; color: #555;
            transition: all .15s; min-height: 36px;
        }
        .period-btn:hover { border-color: #1a1a2e; color: #1a1a2e; }
        .period-btn.active { background: #1a1a2e; color: white; border-color: #1a1a2e; }

        /* Sourcing two-column */
        .two-col { display: flex; gap: 16px; align-items: flex-start; }
        .two-col .col { flex: 1; min-width: 0; }
        .col-heading { font-size: 0.88rem; font-weight: 600; color: #333; margin-bottom: 8px; }
        .intel-table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
        .intel-table th {
            background: #f0f2f5; color: #555; padding: 8px 10px;
            text-align: left; white-space: nowrap; font-weight: 600;
        }
        .intel-table td { padding: 8px 10px; border-bottom: 1px solid #f5f5f5; }
        .intel-table tr:last-child td { border-bottom: none; }
        .pct-good { color: #2e7d32; font-weight: 600; }
        .pct-bad  { color: #c62828; font-weight: 600; }

        /* Funnel */
        .funnel-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
        .funnel { display: flex; align-items: stretch; min-width: min-content; }
        .funnel-stage {
            background: #f8f9ff; border: 1px solid #e8eaf6; border-radius: 8px;
            padding: 16px 12px; text-align: center; flex: 1; min-width: 110px;
        }
        .funnel-count { font-size: 1.8rem; font-weight: 700; color: #1a1a2e; }
        .funnel-label { font-size: 0.72rem; color: #888; margin-top: 4px; line-height: 1.3; }
        .funnel-drop { color: #c62828; font-size: 0.7rem; font-weight: 600; display: block; margin-top: 4px; }
        .funnel-arrow {
            display: flex; align-items: center; padding: 0 6px;
            color: #ccc; font-size: 1.1rem; flex-shrink: 0;
        }

        /* AI panel */
        .ai-header  { font-size: 1rem; font-weight: 700; color: #1a1a2e; }
        .ai-sub     { font-size: 0.78rem; color: #888; margin: 4px 0 14px; }
        .ai-content {
            font-size: 0.9rem; line-height: 1.8; color: #333; white-space: pre-wrap;
            border-top: 1px solid #f0f0f0; padding-top: 14px; min-height: 60px;
        }
        .ai-loading { color: #aaa; font-style: italic; }
        .ai-btn {
            padding: 10px 28px; background: #1a1a2e; color: white; border: none;
            border-radius: 6px; cursor: pointer; font-size: 0.88rem;
            margin-top: 14px; min-height: 44px; transition: background .15s;
        }
        .ai-btn:hover { background: #2d2d4e; }
        .ai-btn:disabled { opacity: 0.5; cursor: not-allowed; }

        /* ═══════════════════════════════════════════
           MOBILE-FIRST RESPONSIVE CSS
           390px mobile | 768px tablet | 1024px+ desktop
        ═══════════════════════════════════════════ */

        /* ── Mobile: up to 767px (default + override) ── */
        @media (max-width: 767px) {
            .topbar { padding: 12px 14px; }
            .topbar h1 { font-size: 1rem; }

            /* 2-per-row stat cards */
            .stats        { padding: 10px 10px 0; gap: 8px; }
            .perf-metrics { padding: 8px 10px 10px; gap: 8px; }
            .stat-card  { flex: 1 1 calc(50% - 4px); padding: 10px 12px; min-width: 0; }
            .perf-card  { flex: 1 1 calc(50% - 4px); padding: 10px 12px; min-width: 0; }
            .stat-card div:first-child,
            .perf-card div:first-child { font-size: 1.4rem !important; }

            /* Stack body vertically */
            .body { flex-direction: column; }
            .sidebar {
                width: 100%; max-height: 220px;
                border-right: none; border-bottom: 1px solid #e8e8e8;
            }
            .main { overflow: visible; padding: 10px; }

            /* Table scrollable */
            .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
            table { min-width: 640px; }
            td, th { padding: 8px 8px; font-size: 0.8rem; }

            /* Sections */
            .sections { padding: 0 10px 24px; gap: 12px; }
            .sec-wrapper { margin: 10px 10px 0; }
            .sec-toggle { padding: 12px 14px; font-size: 0.88rem; }

            /* Period toggle: full-width pills */
            .period-btn { flex: 1; min-height: 44px; text-align: center; font-size: 0.85rem; }

            /* Sourcing: stack */
            .two-col { flex-direction: column; }
            .intel-table { min-width: 260px; }

            /* Funnel: vertical */
            .funnel { flex-direction: column; gap: 2px; min-width: 0; }
            .funnel-stage {
                display: flex; align-items: center; gap: 14px;
                text-align: left; padding: 12px 14px; min-width: 0;
            }
            .funnel-count { font-size: 1.4rem; min-width: 44px; text-align: center; flex-shrink: 0; }
            .funnel-label { font-size: 0.82rem; flex: 1; }
            .funnel-drop  { display: inline; margin-left: 6px; }
            .funnel-arrow { justify-content: center; padding: 0; transform: rotate(90deg); min-height: 20px; }

            /* AI */
            .ai-content { font-size: 0.95rem !important; }
            .ai-btn     { width: 100%; }

            /* Deliver button */
            .deliver-btn { display: block; width: 100%; min-height: 44px; font-size: 0.82rem; margin-top: 4px; }
        }

        /* ── Tablet: 768px–1023px ── */
        @media (min-width: 768px) and (max-width: 1023px) {
            .body { flex-direction: column; }
            .sidebar {
                width: 100%; max-height: 220px;
                border-right: none; border-bottom: 1px solid #e8e8e8;
            }
            .main { overflow: visible; }
            .table-scroll { overflow-x: auto; overflow-y: auto; max-height: 420px; }
            table { min-width: 700px; }
            .perf-card  { flex: 1 1 calc(33.33% - 8px); }
            .funnel { flex-direction: column; gap: 2px; }
            .funnel-stage {
                display: flex; align-items: center; gap: 14px;
                text-align: left; padding: 12px 14px;
            }
            .funnel-count { font-size: 1.4rem; min-width: 44px; text-align: center; flex-shrink: 0; }
            .funnel-label { font-size: 0.85rem; flex: 1; }
            .funnel-drop  { display: inline; margin-left: 6px; }
            .funnel-arrow { justify-content: center; padding: 0; transform: rotate(90deg); min-height: 20px; }
        }

        /* ── Desktop: 1024px+ ── */
        @media (min-width: 1024px) {
            body { height: auto; }
            /* .body no longer needs a fixed height — table self-scrolls via max-height */
            .body { flex-shrink: 0; overflow: visible; }
            .main { overflow: visible; }
            /* Customer list bounded so sidebar doesn't grow unboundedly */
            .customer-list { max-height: 530px; }
            .funnel { flex-direction: row; }
            .funnel-stage { text-align: center; }
            .funnel-arrow { transform: none; }
            .ai-btn { display: inline-block; }
        }
    """

    # ══════════════════════════════════════════════════════
    # JavaScript — data (f-string) + logic (raw string)
    # ══════════════════════════════════════════════════════
    JS_DATA = (
        f"const SOURCING_DATA = {sourcing_json};\n"
        f"const TOTAL_ROWS = {total};\n"
        "const DASH_KEY = new URLSearchParams(window.location.search).get('key') || '';\n"
    )

    JS_LOGIC = r"""
    /* ── Collapsible sections ── */
    function toggleSection(id) {
        var body = document.getElementById('sec-' + id);
        var chev = document.getElementById('chev-' + id);
        if (!body) return;
        var isCollapsed = body.classList.toggle('collapsed');
        chev.style.transform = isCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
    }

    /* ── Existing: filter & search ── */
    let active = null;

    function filterCustomer(el, customer) {
        if (active === customer) { showAll(); return; }
        active = customer;
        document.querySelectorAll('.customer-item').forEach(i => i.classList.remove('active'));
        document.getElementById('all-btn').classList.remove('active');
        el.classList.add('active');
        const rows = document.querySelectorAll('#tbody tr');
        let count = 0;
        rows.forEach(r => {
            const show = r.dataset.customer === customer;
            r.style.display = show ? '' : 'none';
            if (show) count++;
        });
        document.getElementById('table-title').textContent =
            customer + ' — ' + count + ' interacción' + (count !== 1 ? 'es' : '');
        document.getElementById('empty').style.display = count === 0 ? '' : 'none';
    }

    function showAll() {
        active = null;
        document.querySelectorAll('.customer-item').forEach(i => i.classList.remove('active'));
        document.getElementById('all-btn').classList.add('active');
        document.querySelectorAll('#tbody tr').forEach(r => r.style.display = '');
        document.getElementById('table-title').textContent =
            'Todas las interacciones (' + TOTAL_ROWS + ')';
        document.getElementById('empty').style.display = 'none';
    }

    function searchClients(q) {
        document.querySelectorAll('.customer-item').forEach(item => {
            item.style.display = item.dataset.customer.includes(q) ? '' : 'none';
        });
    }

    /* ── Section 2: Sourcing period toggle ── */
    let currentPeriod = 'week';

    function setSourcingPeriod(period) {
        currentPeriod = period;
        document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('[data-period="' + period + '"]').forEach(b => b.classList.add('active'));
        renderSourcingTables(SOURCING_DATA[period]);
    }

    function renderSourcingTables(d) {
        const tp = document.getElementById('top-parts-body');
        if (tp) {
            tp.innerHTML = d.top.length
                ? d.top.map(function(r) {
                    var pctNum = parseInt(r[2]);
                    var cls = pctNum >= 50 ? 'pct-good' : 'pct-bad';
                    return '<tr><td>' + r[0] + '</td>'
                         + '<td style="text-align:center">' + r[1] + '</td>'
                         + '<td style="text-align:center" class="' + cls + '">' + r[2] + '</td></tr>';
                  }).join('')
                : '<tr><td colspan="3" style="text-align:center;color:#aaa;padding:20px">Sin datos este período</td></tr>';
        }
        const gp = document.getElementById('gaps-body');
        if (gp) {
            gp.innerHTML = d.gaps.length
                ? d.gaps.map(function(r) {
                    return '<tr><td>' + r[0] + '</td><td>' + r[1] + '</td>'
                         + '<td style="text-align:center">' + r[2] + '</td></tr>';
                  }).join('')
                : '<tr><td colspan="3" style="text-align:center;color:#2e7d32;padding:20px">Sin gaps — ¡excelente cobertura!</td></tr>';
        }
    }

    /* ── Section 4: AI Insights ── */
    var aiLoaded = false;
    var lastRegen = 0;

    function loadAIInsights() {
        if (aiLoaded) return;
        var el = document.getElementById('ai-content');
        var ts = document.getElementById('ai-timestamp');
        if (!el) return;
        el.innerHTML = '<span class="ai-loading">Cargando análisis...</span>';
        fetch('/dashboard/ai-insights?key=' + DASH_KEY)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.text) {
                    el.textContent = data.text;
                    if (ts && data.generated_at) {
                        var d = new Date(data.generated_at);
                        ts.textContent = 'Generado: ' + d.toLocaleString('es-PA', {timeZone: 'America/Panama'});
                    }
                    aiLoaded = true;
                } else {
                    el.innerHTML = '<span class="ai-loading">Sin análisis aún. Haz clic en Regenerar.</span>';
                }
            })
            .catch(function() {
                el.innerHTML = '<span class="ai-loading">Error al cargar. Intenta de nuevo.</span>';
            });
    }

    function regenerateInsights() {
        var btn = document.getElementById('ai-regen-btn');
        var el  = document.getElementById('ai-content');
        var ts  = document.getElementById('ai-timestamp');
        var now = Date.now();
        if (now - lastRegen < 3600000 && lastRegen > 0) {
            var mins = Math.ceil((3600000 - (now - lastRegen)) / 60000);
            alert('Espera ' + mins + ' min antes de regenerar.');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Generando...';
        el.innerHTML = '<span class="ai-loading">Analizando datos de la semana...</span>';
        fetch('/dashboard/ai-insights?key=' + DASH_KEY + '&force=1')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error === 'cooldown') {
                    var mins = Math.ceil(data.next_in / 60);
                    el.innerHTML = '<span class="ai-loading">Cooldown activo. Espera ' + mins + ' min.</span>';
                } else if (data.text) {
                    el.textContent = data.text;
                    if (ts && data.generated_at) {
                        var d = new Date(data.generated_at);
                        ts.textContent = 'Generado: ' + d.toLocaleString('es-PA', {timeZone: 'America/Panama'});
                    }
                    lastRegen = now;
                    aiLoaded = true;
                }
            })
            .catch(function() {
                el.innerHTML = '<span class="ai-loading">Error al generar. Intenta de nuevo.</span>';
            })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = '🔄 Regenerar análisis';
            });
    }

    /* ── Section 5: Delivery ── */
    function markDelivered(rowTs, customer, btn) {
        if (!confirm('¿Marcar este pedido como entregado?')) return;
        btn.disabled = true;
        btn.textContent = 'Marcando...';
        var fd = new FormData();
        fd.append('row_ts', rowTs);
        fd.append('customer', customer);
        fd.append('key', DASH_KEY);
        fetch('/dashboard/deliver', {method: 'POST', body: fd})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    var cell = btn.parentElement;
                    var ts   = (d.ts || '').substring(0, 16);
                    cell.innerHTML = '<span class="delivered-tag">✅ Entregado<small>' + ts + '</small></span>';
                } else {
                    btn.disabled = false;
                    btn.textContent = '✓ Marcar entregado';
                    alert('Error: ' + (d.error || 'intenta de nuevo'));
                }
            })
            .catch(function() {
                btn.disabled = false;
                btn.textContent = '✓ Marcar entregado';
                alert('Error de conexión.');
            });
    }

    /* ── Init ── */
    document.addEventListener('DOMContentLoaded', function() {
        setSourcingPeriod('week');
        var aiPanel = document.getElementById('ai-panel');
        if (aiPanel && 'IntersectionObserver' in window) {
            var obs = new IntersectionObserver(function(entries) {
                if (entries[0].isIntersecting) { loadAIInsights(); obs.disconnect(); }
            }, {rootMargin: '300px'});
            obs.observe(aiPanel);
        } else {
            setTimeout(loadAIInsights, 2000);
        }
    });
    """

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AutoParts Dashboard</title>
    <style>{CSS}</style>
</head>
<body>

<div class="topbar">
    <h1>📊 AutoParts Dashboard</h1>
    <small>Auto-refresh cada 60s</small>
</div>

<!-- Existing stat cards -->
<div class="stats">{stats_html}</div>

<!-- Section 1: Performance metrics bar -->
<div class="perf-metrics">{perf_html}</div>

<!-- Interactions: collapsible wrapper -->
<div class="sec-wrapper">
    <div class="sec-toggle" onclick="toggleSection('interactions')">
        <span class="sec-title">
            📋 Todas las interacciones
            <span class="sec-count">({total} registros)</span>
        </span>
        <span class="chevron" id="chev-interactions">▼</span>
    </div>
    <div class="sec-body" id="sec-interactions">
        <div class="body">
            <div class="sidebar">
                <div class="sidebar-header">
                    <input type="text" id="search" placeholder="Buscar cliente..."
                           oninput="searchClients(this.value)">
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
                    <div class="table-scroll">
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
                                    <th>Entrega</th>
                                </tr>
                            </thead>
                            <tbody id="tbody">{row_html}</tbody>
                        </table>
                    </div>
                    <div class="empty" id="empty" style="display:none">Sin resultados</div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Sections 2–4 -->
<div class="sections">

    <!-- Section 2: Sourcing Intelligence -->
    <div class="section-card">
        <div class="sec-toggle" onclick="toggleSection('sourcing')">
            <span class="sec-title">🔍 Inteligencia de Sourcing</span>
            <span class="chevron" id="chev-sourcing">▼</span>
        </div>
        <div class="sec-body" id="sec-sourcing" style="padding:16px 20px 20px">
            <div class="section-sub" style="margin-top:4px">Piezas más solicitadas y gaps de inventario</div>
            <div class="period-toggle">
                <button class="period-btn active" data-period="week"  onclick="setSourcingPeriod('week')">Esta semana</button>
                <button class="period-btn"        data-period="month" onclick="setSourcingPeriod('month')">Este mes</button>
                <button class="period-btn"        data-period="all"   onclick="setSourcingPeriod('all')">Todo</button>
            </div>
            <div class="two-col">
                <div class="col">
                    <div class="col-heading">📦 Piezas más solicitadas</div>
                    <div style="overflow-x:auto;overflow-y:auto;max-height:320px;-webkit-overflow-scrolling:touch">
                        <table class="intel-table">
                            <thead><tr><th>Pieza</th><th>Solicitudes</th><th>Éxito %</th></tr></thead>
                            <tbody id="top-parts-body"></tbody>
                        </table>
                    </div>
                </div>
                <div class="col">
                    <div class="col-heading">❌ Gaps de inventario</div>
                    <div style="overflow-x:auto;overflow-y:auto;max-height:320px;-webkit-overflow-scrolling:touch">
                        <table class="intel-table">
                            <thead><tr><th>Pieza</th><th>Make</th><th>Sin resultado</th></tr></thead>
                            <tbody id="gaps-body"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Section 3: Conversion Funnel -->
    <div class="section-card">
        <div class="sec-toggle" onclick="toggleSection('funnel')">
            <span class="sec-title">🔁 Embudo de Conversión</span>
            <span class="chevron" id="chev-funnel">▼</span>
        </div>
        <div class="sec-body" id="sec-funnel" style="padding:16px 20px 20px">
            <div class="section-sub" style="margin-bottom:14px">Del primer mensaje a la entrega</div>
            <div class="funnel-wrap">
                <div class="funnel">{funnel_html}</div>
            </div>
        </div>
    </div>

    <!-- Section 4: AI Insights -->
    <div class="section-card" id="ai-panel">
        <div class="sec-toggle" onclick="toggleSection('ai')">
            <span class="sec-title">📊 Análisis Semanal — Generado por IA</span>
            <span class="chevron" id="chev-ai">▼</span>
        </div>
        <div class="sec-body" id="sec-ai" style="padding:16px 20px 20px">
            <div class="ai-sub" id="ai-timestamp" style="margin-bottom:12px">Cargando...</div>
            <div class="ai-content" id="ai-content">
                <span class="ai-loading">El análisis se cargará automáticamente...</span>
            </div>
            <button class="ai-btn" id="ai-regen-btn" onclick="regenerateInsights()">
                🔄 Regenerar análisis
            </button>
        </div>
    </div>

</div>

<script>{JS_DATA}{JS_LOGIC}</script>
<meta http-equiv="refresh" content="60">
</body>
</html>"""
