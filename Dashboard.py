import html
from datetime import datetime, timedelta
from collections import defaultdict
import streamlit as st
import plotly.graph_objects as go
import plotly.io as pio

st.set_page_config(page_title="EPC Project Controls Executive Dashboard", layout="wide")
st.title("EPC Project Controls Executive Dashboard (Baseline vs. Update XER)")

# ---------------------------------------------------------
# 1. XER PARSER & HELPERS
# ---------------------------------------------------------
def parse_xer(content):
    tables = {}
    current_table = None
    fields = []
    for line in content.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rec = parts[0]
        if rec == "%T":
            current_table = parts[1].strip()
            tables[current_table] = []
        elif rec == "%F":
            fields = [f.strip() for f in parts[1:]]
        elif rec == "%R" and current_table:
            tables[current_table].append(dict(zip(fields, parts[1:])))
    return tables

def parse_date(date_str):
    if not date_str or not date_str.strip():
        return None
    clean = date_str.strip().split()[0]
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None

def get_pct_complete(task):
    stype = task.get("complete_pct_type", "CP_Drtn")
    if stype == "CP_Phys":
        return float(task.get("phys_complete_pct") or 0) / 100.0
    elif stype == "CP_Units":
        tgt = float(task.get("target_work_qty") or 0) + float(task.get("target_equip_qty") or 0)
        act = float(task.get("act_work_qty") or 0) + float(task.get("act_equip_qty") or 0)
        return (act / tgt) if tgt > 0 else 0.0
    else:
        return float(task.get("drtn_complete_pct") or 0) / 100.0

def extract_activity_codes(tables):
    """Extracts Activity Code Types and maps task_id -> {type_id: 'Code - Description'}."""
    actv_types = {}
    for row in tables.get("ACTVTYPE", []):
        tid = row.get("actv_code_type_id")
        tname = row.get("actv_code_type", "").strip()
        if tid and tname:
            actv_types[tid] = tname

    code_details = {}
    code_order = {}
    for row in tables.get("ACTVCODE", []):
        cid = row.get("actv_code_id")
        tid = row.get("actv_code_type_id")
        short = row.get("short_name", "").strip()
        desc = row.get("actv_code_name", "").strip()
        seq = int(row.get("seq_num") or 9999)
        label = f"{short} - {desc}" if (short and desc) else (short or desc)
        code_details[cid] = (tid, label)
        code_order[label] = seq

    task_code_map = defaultdict(dict)
    for row in tables.get("TASKACTV", []):
        task_id = row.get("task_id")
        cid = row.get("actv_code_id")
        if task_id and cid in code_details:
            tid, label = code_details[cid]
            task_code_map[task_id][tid] = label

    return actv_types, task_code_map, code_order

# ---------------------------------------------------------
# 2. SIDEBAR CONTROLS & FILE UPLOADS
# ---------------------------------------------------------
with st.sidebar:
    st.header("1. Upload Schedules")
    bl_file = st.file_uploader("Baseline Schedule (.xer)", type="xer", key="bl")
    up_file = st.file_uploader("Current Update / Forecast (.xer)", type="xer", key="up")

if not bl_file or not up_file:
    st.info("Upload both your **Baseline (.xer)** and **Current Update (.xer)** files in the left sidebar to generate the dashboard.")
    st.stop()

bl_tables = parse_xer(bl_file.getvalue().decode("cp1252", errors="replace"))
up_tables = parse_xer(up_file.getvalue().decode("cp1252", errors="replace"))

bl_types, bl_task_codes, bl_code_order = extract_activity_codes(bl_tables)
up_types, up_task_codes, up_code_order = extract_activity_codes(up_tables)

# Combine Activity Code Types across Baseline & Update, defaulting to G02 - JSTEPS
all_type_names = sorted(set(up_types.values()) | set(bl_types.values()))
default_idx = 0
for idx, name in enumerate(all_type_names):
    if "G02" in name.upper() or "JSTEPS" in name.upper():
        default_idx = idx
        break

with st.sidebar:
    st.header("2. Grouping & Parameters")
    selected_code_type = st.selectbox(
        "Group S-Curves by Activity Code:",
        all_type_names if all_type_names else ["No Activity Codes Found"],
        index=default_idx
    )
    basis = st.radio("Weighting Basis:", ["Cost (Currency)", "Labor / Material Units"])
    use_cost = "Cost" in basis
    hours_per_day = st.number_input("Hours per Working Day:", min_value=1, max_value=24, value=8)
    near_crit_days = st.slider("Near-Critical Threshold (Days):", min_value=1, max_value=30, value=10)
    near_crit_hrs = near_crit_days * hours_per_day

# Find matching actv_code_type_id in Baseline and Update XERs
bl_target_tid = next((k for k, v in bl_types.items() if v == selected_code_type), None)
up_target_tid = next((k for k, v in up_types.items() if v == selected_code_type), None)

# ---------------------------------------------------------
# 3. PROCESS BASELINE & UPDATE DATA
# ---------------------------------------------------------
up_proj = up_tables.get("PROJECT", [{}])[0]
proj_name = up_proj.get("proj_short_name", "Project Schedule")
data_date = parse_date(up_proj.get("last_recalc_date")) or datetime.today()

def map_resources(rsrc_rows):
    tgt_map, act_map, rem_map = defaultdict(float), defaultdict(float), defaultdict(float)
    for r in rsrc_rows:
        tid = r.get("task_id")
        if use_cost:
            tgt_map[tid] += float(r.get("target_cost") or 0)
            act_map[tid] += float(r.get("act_reg_cost") or 0) + float(r.get("act_ot_cost") or 0)
            rem_map[tid] += float(r.get("remain_cost") or 0)
        else:
            tgt_map[tid] += float(r.get("target_qty") or 0)
            act_map[tid] += float(r.get("act_reg_qty") or 0) + float(r.get("act_ot_qty") or 0)
            rem_map[tid] += float(r.get("remain_qty") or 0)
    return tgt_map, act_map, rem_map

bl_tgt, _, _ = map_resources(bl_tables.get("TASKRSRC", []))
up_tgt, up_act, up_rem = map_resources(up_tables.get("TASKRSRC", []))

# Discover all unique phase values assigned to activities
discovered_phases = set()
for t in bl_tables.get("TASK", []):
    val = bl_task_codes.get(t.get("task_id"), {}).get(bl_target_tid)
    if val:
        discovered_phases.add(val)
for t in up_tables.get("TASK", []):
    val = up_task_codes.get(t.get("task_id"), {}).get(up_target_tid)
    if val:
        discovered_phases.add(val)

# Sort phases by P6 sequence number (e.g., P2, P3, P4, P5, P6)
combined_order = {**bl_code_order, **up_code_order}
sorted_subphases = sorted(discovered_phases, key=lambda x: (combined_order.get(x, 9999), x))
phases = ["Overall"] + sorted_subphases

pv_daily = {p: defaultdict(float) for p in phases}
ev_daily = {p: defaultdict(float) for p in phases}
ac_daily = {p: defaultdict(float) for p in phases}
fc_daily = {p: defaultdict(float) for p in phases}
phase_kpis = {p: {"BAC": 0.0, "PV_DD": 0.0, "EV": 0.0, "AC": 0.0, "EAC": 0.0, "BL_Finish": None, "FC_Finish": None} for p in phases}

# A. Spread Baseline PV
bl_task_lookup = {}
for t in bl_tables.get("TASK", []):
    if t.get("task_type") in ("TT_Mile", "TT_FinMile", "TT_WBS"):
        continue
    tid = t.get("task_id")
    code = t.get("task_code", "")
    val = bl_tgt[tid]
    s_dt = parse_date(t.get("target_start_date") or t.get("early_start_date"))
    f_dt = parse_date(t.get("target_end_date") or t.get("early_end_date"))
    if not s_dt or not f_dt:
        continue

    ph = bl_task_codes.get(tid, {}).get(bl_target_tid, "Unassigned")
    bl_task_lookup[code] = {"bl_start": s_dt, "bl_finish": f_dt, "bl_val": val, "phase": ph}

    dur = max((f_dt - s_dt).days + 1, 1)
    rate = val / dur
    target_groups = ["Overall", ph] if ph in phase_kpis else ["Overall"]

    for grp in target_groups:
        phase_kpis[grp]["BAC"] += val
        if phase_kpis[grp]["BL_Finish"] is None or f_dt > phase_kpis[grp]["BL_Finish"]:
            phase_kpis[grp]["BL_Finish"] = f_dt
        for i in range(dur):
            d_dt = s_dt + timedelta(days=i)
            d_str = d_dt.strftime("%Y-%m-%d")
            pv_daily[grp][d_str] += rate
            if d_dt <= data_date:
                phase_kpis[grp]["PV_DD"] += rate

# B. Spread Update Actuals (EV & AC) and Forecast (ETC) + Diagnostics
critical_list = []
near_critical_list = []
early_warnings = []
delayed_activities = []

for t in up_tables.get("TASK", []):
    if t.get("task_type") in ("TT_Mile", "TT_FinMile", "TT_WBS"):
        continue
    tid = t.get("task_id")
    code = t.get("task_code", "")
    name = t.get("task_name", "")
    status = t.get("status_code", "TK_NotStart")
    ph = up_task_codes.get(tid, {}).get(up_target_tid) or bl_task_lookup.get(code, {}).get("phase", "Unassigned")
    target_groups = ["Overall", ph] if ph in phase_kpis else ["Overall"]

    bac_task = bl_task_lookup.get(code, {}).get("bl_val", up_tgt[tid])
    pct = 1.0 if status == "TK_Complete" else get_pct_complete(t)
    ev_val = bac_task * pct
    ac_val = up_act[tid]
    rem_val = 0.0 if status == "TK_Complete" else up_rem[tid]
    eac_val = ac_val + rem_val

    act_s = parse_date(t.get("act_start_date"))
    act_f = parse_date(t.get("act_end_date"))
    early_s = parse_date(t.get("early_start_date"))
    early_f = parse_date(t.get("early_end_date"))
    bl_f = bl_task_lookup.get(code, {}).get("bl_finish") or parse_date(t.get("target_end_date"))

    if status in ("TK_Active", "TK_Complete") and act_s:
        end_act = act_f if (status == "TK_Complete" and act_f) else data_date
        end_act = max(end_act, act_s)
        dur_act = max((end_act - act_s).days + 1, 1)
        ev_rate = ev_val / dur_act
        ac_rate = ac_val / dur_act
        for i in range(dur_act):
            d_str = (act_s + timedelta(days=i)).strftime("%Y-%m-%d")
            for grp in target_groups:
                ev_daily[grp][d_str] += ev_rate
                ac_daily[grp][d_str] += ac_rate

    if status != "TK_Complete" and early_s and early_f:
        fc_start = max(early_s, data_date)
        fc_end = max(early_f, fc_start)
        dur_fc = max((fc_end - fc_start).days + 1, 1)
        rem_rate = rem_val / dur_fc
        for i in range(dur_fc):
            d_str = (fc_start + timedelta(days=i)).strftime("%Y-%m-%d")
            for grp in target_groups:
                fc_daily[grp][d_str] += rem_rate

    fc_finish_dt = act_f if status == "TK_Complete" else early_f
    for grp in target_groups:
        phase_kpis[grp]["EV"] += ev_val
        phase_kpis[grp]["AC"] += ac_val
        phase_kpis[grp]["EAC"] += eac_val
        if fc_finish_dt and (phase_kpis[grp]["FC_Finish"] is None or fc_finish_dt > phase_kpis[grp]["FC_Finish"]):
            phase_kpis[grp]["FC_Finish"] = fc_finish_dt

    tf_hrs = float(t.get("total_float_hr_cnt") or 0)
    tf_days = round(tf_hrs / hours_per_day, 1)
    slip_days = (fc_finish_dt - bl_f).days if (fc_finish_dt and bl_f) else 0

    if status != "TK_Complete":
        item_info = {
            "Activity ID": code,
            "Activity Name": name,
            "Stage / Code": ph,
            "Status": "In Progress" if status == "TK_Active" else "Not Started",
            "Total Float (Days)": tf_days,
            "Forecast Finish": fc_finish_dt.strftime("%Y-%m-%d") if fc_finish_dt else "N/A",
            "Baseline Finish": bl_f.strftime("%Y-%m-%d") if bl_f else "N/A",
            "Variance (Days)": slip_days
        }
        if tf_hrs <= 0:
            critical_list.append(item_info)
        elif tf_hrs <= near_crit_hrs:
            near_critical_list.append(item_info)

        if status == "TK_NotStart" and early_s and early_s <= data_date + timedelta(days=14) and tf_days <= near_crit_days:
            early_warnings.append(f"Imminent Low-Float Start: {code} ({name}) [{ph}] starts within 14 days with {tf_days}d float.")
        if status == "TK_Active" and pct < 0.25 and early_f and (early_f - data_date).days < 10 and rem_val > 0:
            early_warnings.append(f"Stalled Progress Risk: {code} ({name}) [{ph}] is at {pct*100:.0f}% complete with <10 days remaining.")
        if t.get("cstr_type") in ("CS_MANDFIN", "CS_MANDSTART", "CS_MEO", "CS_MSO"):
            early_warnings.append(f"Hard Constraint Alert: {code} ({name}) [{ph}] has constraint {t.get('cstr_type')} on {t.get('cstr_date', '')[:10]}.")

    if slip_days > 0:
        delayed_activities.append({
            "Activity ID": code,
            "Activity Name": name,
            "Stage / Code": ph,
            "Baseline Finish": bl_f.strftime("%Y-%m-%d"),
            "Actual / Forecast Finish": fc_finish_dt.strftime("%Y-%m-%d"),
            "Slippage (Days)": slip_days,
            "Total Float (Days)": tf_days
        })

# ---------------------------------------------------------
# 4. BUILD CHARTS, NARRATIVES & EXPORTABLE REPORT
# ---------------------------------------------------------
def build_phase_artifacts(phase_name):
    k = phase_kpis[phase_name]
    bac, pv_dd, ev, ac, eac = k["BAC"], k["PV_DD"], k["EV"], k["AC"], k["EAC"]
    spi = (ev / pv_dd) if pv_dd > 0 else 1.0
    cpi = (ev / ac) if ac > 0 else 1.0
    sv = ev - pv_dd
    cv = ev - ac
    pct_plan = (pv_dd / bac * 100) if bac > 0 else 0.0
    pct_earned = (ev / bac * 100) if bac > 0 else 0.0

    bl_end = k["BL_Finish"]
    fc_end = k["FC_Finish"]
    end_var = (fc_end - bl_end).days if (bl_end and fc_end) else 0

    all_dates = sorted(set(pv_daily[phase_name].keys()) | set(ev_daily[phase_name].keys()) | set(fc_daily[phase_name].keys()))
    dd_str = data_date.strftime("%Y-%m-%d")

    cum_pv, cum_ev, cum_ac = 0.0, 0.0, 0.0
    x_pv, y_pv, x_ev, y_ev, x_ac, y_ac, x_fc, y_fc = [], [], [], [], [], [], [], []

    for d in all_dates:
        cum_pv += pv_daily[phase_name][d]
        x_pv.append(d)
        y_pv.append(round(cum_pv, 2))
        if d <= dd_str:
            cum_ev += ev_daily[phase_name][d]
            cum_ac += ac_daily[phase_name][d]
            x_ev.append(d)
            y_ev.append(round(cum_ev, 2))
            x_ac.append(d)
            y_ac.append(round(cum_ac, 2))
        else:
            if not x_fc:
                x_fc.append(dd_str)
                y_fc.append(round(cum_ev, 2))
            cum_ev += fc_daily[phase_name][d]
            x_fc.append(d)
            y_fc.append(round(cum_ev, 2))

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x_pv, y=y_pv, name="Baseline Planned (PV)", line=dict(color="#1f77b4", width=3)))
    fig.add_trace(go.Scatter(x=x_ev, y=y_ev, name="Actual Earned (EV)", line=dict(color="#2ca02c", width=3)))
    fig.add_trace(go.Scatter(x=x_ac, y=y_ac, name="Actual Spent (AC)", line=dict(color="#d62728", width=2, dash="dot")))
    fig.add_trace(go.Scatter(x=x_fc, y=y_fc, name="Forecast Remaining (ETC)", line=dict(color="#ff7f0e", width=3, dash="dash")))
    fig.add_vline(x=dd_str, line_width=2, line_dash="dash", line_color="gray")
    fig.add_annotation(x=dd_str, y=max(y_pv or [0]), text=f"Data Date ({dd_str})", showarrow=False, yshift=10)
    fig.update_layout(
        title=f"{phase_name} S-Curve — Baseline vs. Actual & Forecast",
        xaxis_title="Timeline",
        yaxis_title=f"Cumulative {basis}",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )

    spi_status = "ahead of schedule" if spi >= 1.02 else ("on track" if spi >= 0.95 else ("behind schedule" if spi >= 0.85 else "critically behind schedule"))
    cpi_status = "favorable efficiency" if cpi >= 1.0 else "unfavorable overrun"
    slip_msg = f"projects a {end_var}-day delay against the baseline finish of {bl_end.strftime('%Y-%m-%d')}" if end_var > 0 else (f"forecasts completion {abs(end_var)} days early" if end_var < 0 else "holds the baseline completion date")

    phase_delays = [d for d in delayed_activities if phase_name == "Overall" or d["Stage / Code"] == phase_name]
    phase_delays.sort(key=lambda x: x["Slippage (Days)"], reverse=True)
    top_drivers = ", ".join([f"{d['Activity ID']} (+{d['Slippage (Days)']}d)" for d in phase_delays[:3]]) or "None"

    narrative_bullets = [
        f"Performance Summary: As of Data Date {dd_str}, {phase_name} achieved {pct_earned:.1f}% actual progress vs. {pct_plan:.1f}% planned (SPI: {spi:.2f} — {spi_status}; SV: {sv:+,.0f}).",
        f"Cost / Productivity: Actual spent/hours stand at {ac:,.0f} vs. {ev:,.0f} earned (CPI: {cpi:.2f} — {cpi_status}; EAC: {eac:,.0f} vs. BAC: {bac:,.0f}).",
        f"Completion Forecast: {phase_name} {slip_msg} (Forecast Finish: {fc_end.strftime('%Y-%m-%d') if fc_end else 'N/A'}).",
        f"Primary Delay Drivers: {top_drivers}."
    ]

    metrics = {
        "pct_plan": pct_plan, "pct_earned": pct_earned, "spi": spi, "sv": sv,
        "cpi": cpi, "cv": cv, "bl_end": bl_end, "fc_end": fc_end, "end_var": end_var
    }
    return fig, metrics, narrative_bullets

# Pre-compute artifacts for all phases so they render in UI and export to HTML
phase_artifacts = {p: build_phase_artifacts(p) for p in phases}

def generate_html_report():
    dd_str = data_date.strftime("%Y-%m-%d")
    sections_html = []

    for idx, p in enumerate(phases):
        fig, m, bullets = phase_artifacts[p]
        chart_html = pio.to_html(fig, full_html=False, include_plotlyjs=("cdn" if idx == 0 else False))
        bl_str = m["bl_end"].strftime("%Y-%m-%d") if m["bl_end"] else "N/A"
        fc_str = m["fc_end"].strftime("%Y-%m-%d") if m["fc_end"] else "N/A"
        bullets_html = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)

        sections_html.append(f"""
        <div class="card">
            <h2>{html.escape(p)}</h2>
            <div class="kpi-grid">
                <div class="kpi"><span class="lbl">Planned %</span><span class="val">{m['pct_plan']:.1f}%</span></div>
                <div class="kpi"><span class="lbl">Earned %</span><span class="val">{m['pct_earned']:.1f}%</span></div>
                <div class="kpi"><span class="lbl">SPI</span><span class="val">{m['spi']:.2f}</span></div>
                <div class="kpi"><span class="lbl">CPI</span><span class="val">{m['cpi']:.2f}</span></div>
                <div class="kpi"><span class="lbl">Baseline Finish</span><span class="val">{bl_str}</span></div>
                <div class="kpi"><span class="lbl">Forecast Finish</span><span class="val">{fc_str} ({-m['end_var']:+d}d)</span></div>
            </div>
            {chart_html}
            <h3>Schedule Narrative Analysis</h3>
            <ul>{bullets_html}</ul>
        </div>
        """)

    def dict_list_to_table(rows, empty_msg):
        if not rows:
            return f"<p>{html.escape(empty_msg)}</p>"
        headers = list(rows[0].keys())
        th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
        trs = []
        for r in rows:
            tds = "".join(f"<td>{html.escape(str(r[h]))}</td>" for h in headers)
            trs.append(f"<tr>{tds}</tr>")
        return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"

    crit_sorted = sorted(critical_list, key=lambda x: x["Total Float (Days)"])
    near_sorted = sorted(near_critical_list, key=lambda x: x["Total Float (Days)"])
    delay_sorted = sorted(delayed_activities, key=lambda x: x["Slippage (Days)"], reverse=True)
    warn_html = "<ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in early_warnings) + "</ul>" if early_warnings else "<p>No early warnings detected.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(proj_name)} — Executive Schedule Report ({dd_str})</title>
<style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 30px; background: #f4f6f9; color: #1f2937; }}
    h1 {{ color: #0f172a; border-bottom: 3px solid #1f77b4; padding-bottom: 10px; }}
    h2 {{ color: #1e3a8a; margin-top: 0; }}
    .meta {{ color: #4b5563; margin-bottom: 24px; font-size: 14px; }}
    .card {{ background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.08); margin-bottom: 28px; page-break-inside: avoid; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 18px; }}
    .kpi {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; text-align: center; }}
    .kpi .lbl {{ display: block; font-size: 12px; color: #64748b; text-transform: uppercase; }}
    .kpi .val {{ display: block; font-size: 18px; font-weight: bold; color: #0f172a; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 8px 10px; text-align: left; }}
    th {{ background: #1e3a8a; color: #fff; }}
    tr:nth-child(even) {{ background: #f8fafc; }}
</style>
</head>
<body>
    <h1>Project Controls Executive Schedule Report — {html.escape(proj_name)}</h1>
    <div class="meta">
        <strong>Data Date:</strong> {dd_str} &nbsp;|&nbsp;
        <strong>Activity Code Grouping:</strong> {html.escape(selected_code_type)} &nbsp;|&nbsp;
        <strong>Weighting Basis:</strong> {html.escape(basis)} &nbsp;|&nbsp;
        <strong>Near-Critical Threshold:</strong> &le; {near_crit_days} days
    </div>
    {''.join(sections_html)}
    <div class="card">
        <h2>Critical Path Activities (Total Float &le; 0 Days) — {len(crit_sorted)}</h2>
        {dict_list_to_table(crit_sorted, "No open critical path activities.")}
    </div>
    <div class="card">
        <h2>Near-Critical Activities (0 &lt; Total Float &le; {near_crit_days} Days) — {len(near_sorted)}</h2>
        {dict_list_to_table(near_sorted, "No near-critical activities within threshold.")}
    </div>
    <div class="card">
        <h2>Early Warning Signs ({len(early_warnings)})</h2>
        {warn_html}
    </div>
    <div class="card">
        <h2>Delayed Activities vs. Baseline ({len(delay_sorted)})</h2>
        {dict_list_to_table(delay_sorted, "No delayed activities.")}
    </div>
</body>
</html>"""

with st.sidebar:
    st.header("3. Export Executive Report")
    report_bytes = generate_html_report().encode("utf-8")
    st.download_button(
        label="📥 Download Executive Report (HTML)",
        data=report_bytes,
        file_name=f"{proj_name}_Executive_Report_{data_date.strftime('%Y%m%d')}.html",
        mime="text/html",
        use_container_width=True
    )

# ---------------------------------------------------------
# 5. MAIN DASHBOARD TABS
# ---------------------------------------------------------
st.caption(
    f"Project: **{proj_name}** | Data Date: **{data_date.strftime('%Y-%m-%d')}** | "
    f"Grouping: **{selected_code_type}** | Near-Critical: **<= {near_crit_days}d**"
)

tab_labels = [f"📊 {p}" for p in phases] + ["🔥 Critical & Near-Critical", "⚠️ Early Warnings & Delays"]
tabs = st.tabs(tab_labels)

for idx, p in enumerate(phases):
    with tabs[idx]:
        fig, m, bullets = phase_artifacts[p]
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Planned % (DD)", f"{m['pct_plan']:.1f}%")
        c2.metric("Earned % (Actual)", f"{m['pct_earned']:.1f}%", f"{m['pct_earned'] - m['pct_plan']:+.1f}%")
        c3.metric("SPI (Schedule)", f"{m['spi']:.2f}", f"{m['sv']:+,.0f}")
        c4.metric("CPI (Cost/Prod)", f"{m['cpi']:.2f}", f"{m['cv']:+,.0f}")
        c5.metric("Baseline Finish", m["bl_end"].strftime("%Y-%m-%d") if m["bl_end"] else "N/A")
        c6.metric("Forecast Finish", m["fc_end"].strftime("%Y-%m-%d") if m["fc_end"] else "N/A", f"{-m['end_var']:+d}d" if m["end_var"] != 0 else "0d")

        st.plotly_chart(fig, use_container_width=True)
        st.subheader(f"{p} — Schedule Narrative Analysis")
        for b in bullets:
            st.markdown(f"* **{b.split(':', 1)[0]}:**{b.split(':', 1)[1]}")

with tabs[-2]:
    st.subheader(f"Critical Path Activities (Total Float <= 0 Days) — {len(critical_list)} Activities")
    if critical_list:
        st.dataframe(sorted(critical_list, key=lambda x: x["Total Float (Days)"]), use_container_width=True)
    else:
        st.success("No open activities with Total Float <= 0 days found.")

    st.subheader(f"Near-Critical Activities (0 < Total Float <= {near_crit_days} Days) — {len(near_critical_list)} Activities")
    if near_critical_list:
        st.dataframe(sorted(near_critical_list, key=lambda x: x["Total Float (Days)"]), use_container_width=True)
    else:
        st.info(f"No open activities within the {near_crit_days}-day near-critical threshold.")

with tabs[-1]:
    col_w, col_d = st.columns([1, 1.3])
    with col_w:
        st.subheader(f"Early Warning Signs ({len(early_warnings)})")
        if early_warnings:
            for w in early_warnings[:25]:
                st.warning(w)
        else:
            st.success("No immediate early warning triggers detected.")
    with col_d:
        st.subheader(f"Delayed Activities vs. Baseline ({len(delayed_activities)})")
        if delayed_activities:
            st.dataframe(sorted(delayed_activities, key=lambda x: x["Slippage (Days)"], reverse=True), use_container_width=True)
        else:
            st.success("All activities are tracking on or ahead of baseline finish dates.")