import os
import subprocess
import sys

import pandas as pd
import streamlit as st

st.set_page_config(page_title="SmartCloud Optimizer", layout="wide")
st.title("SmartCloud Optimizer")

# Where the data files live: the sample data (project folder) or the user's upload (user_data folder)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(BASE_DIR, "user_data")
using_user_data = st.session_state.get("use_user_data", False) and os.path.isdir(USER_DIR)
DATA_DIR = USER_DIR if using_user_data else BASE_DIR
os.chdir(DATA_DIR)   # every file the app reads below now comes from DATA_DIR
if using_user_data:
    st.info("Showing YOUR uploaded data. Open the Upload Data tab to switch back to the sample data.")


@st.cache_data
def load_data(folder):
    inv = pd.read_csv("inventory.csv")
    met = pd.read_parquet("metrics.parquet")
    recs = pd.read_csv("recommendations.csv")
    alerts = pd.read_csv("alerts.csv")
    cost_hist = pd.read_csv("cost_history.csv").fillna("")
    drivers = pd.read_csv("cost_drivers.csv")
    budget = int(pd.read_csv("budget.csv")["monthly_budget"].iloc[0])
    return inv, met, recs, alerts, cost_hist, drivers, budget


inventory, metrics, recs, alerts, cost_hist, drivers, budget = load_data(DATA_DIR)

# ----- Sidebar filters -----
st.sidebar.header("Filters")
all_regions = sorted(inventory["region"].unique())
all_envs = sorted(inventory["environment"].unique())
all_teams = sorted(inventory["team"].unique())

regions = st.sidebar.multiselect("Region", all_regions, default=all_regions)
envs = st.sidebar.multiselect("Environment", all_envs, default=all_envs)
teams = st.sidebar.multiselect("Team", all_teams, default=all_teams)

filtered = inventory[
    inventory["region"].isin(regions)
    & inventory["environment"].isin(envs)
    & inventory["team"].isin(teams)
]

if filtered.empty:
    st.warning("No machines match these filters.")
    st.stop()

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    ["Utilization", "Recommendations", "Alerts & Insights", "What-if Simulator",
     "Assumptions", "Validation", "Upload Data"])

# ================= Tab 1: Utilization =================
with tab1:
    c1, c2 = st.columns(2)
    c1.metric("Machines", len(filtered))
    c2.metric("Monthly cost (Rs)", f"{filtered['monthly_cost'].sum():,}")
    fleet = metrics[metrics["resource_id"].isin(filtered["resource_id"])]
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Avg CPU %", round(fleet["cpu_pct"].mean(), 1))
    f2.metric("Avg memory %", round(fleet["mem_pct"].mean(), 1))
    if "disk_pct" in fleet.columns:
        f3.metric("Avg disk used %", round(fleet["disk_pct"].mean(), 1))
    if "net_mbps" in fleet.columns:
        f4.metric("Avg network (Mbps)", round(fleet["net_mbps"].mean(), 1))

    st.dataframe(filtered, use_container_width=True)

    if os.path.exists("orphans.csv"):
        orph_view = pd.read_csv("orphans.csv")
        orph_view = orph_view[orph_view["region"].isin(regions)]
        with st.expander(f"Resources not attached to any VM "
                         f"({len(orph_view)} orphaned volumes and public IPs)"):
            st.dataframe(orph_view, hide_index=True, use_container_width=True)

    st.subheader("Drill down: one machine's history")
    choice = st.selectbox("Pick a machine", filtered["resource_id"])
    history = metrics[metrics["resource_id"] == choice].set_index("timestamp")

    m1, m2, m3 = st.columns(3)
    m1.metric("Average CPU %", round(history["cpu_pct"].mean(), 1))
    m2.metric("p95 CPU %", round(history["cpu_pct"].quantile(0.95), 1))
    m3.metric("p99 CPU %", round(history["cpu_pct"].quantile(0.99), 1))
    st.line_chart(history[["cpu_pct", "mem_pct"]])
    d1, d2 = st.columns(2)
    if "disk_pct" in history.columns:
        d1.write("**Storage: disk used %**")
        d1.line_chart(history[["disk_pct"]])
    net_cols = [c for c in ["net_in_mbps", "net_out_mbps"] if c in history.columns]
    if not net_cols and "net_mbps" in history.columns:
        net_cols = ["net_mbps"]
    if net_cols:
        d2.write("**Network (Mbps)**")
        d2.line_chart(history[net_cols])
    if "iops" in history.columns:
        st.write("**Disk IOPS**")
        st.line_chart(history[["iops"]])

# ================= Tab 2: Recommendations =================
with tab2:
    view = recs[
        recs["resource_id"].isin(filtered["resource_id"])
        | ((recs["environment"] == "orphan") & recs["region"].isin(regions))
    ].copy()

    if view.empty:
        st.info("No recommendations for the selected filters.")
    else:
        st.write("Untick any recommendation you reject. The totals update.")
        view.insert(0, "accept", True)
        edited = st.data_editor(
            view,
            column_config={"accept": st.column_config.CheckboxColumn("Accept")},
            disabled=[c for c in view.columns if c != "accept"],
            hide_index=True,
            use_container_width=True,
        )
        accepted = edited[edited["accept"]]

        current_total = int(filtered["monthly_cost"].sum()
                            + view[view["environment"] == "orphan"]["current_cost"].sum())
        saving = int(accepted["saving"].sum())

        a, b, c = st.columns(3)
        a.metric("Current monthly cost (Rs)", f"{current_total:,}")
        b.metric("After accepted changes (Rs)", f"{current_total - saving:,}",
                 delta=f"-{saving:,}", delta_color="inverse")
        c.metric("Yearly saving (Rs)", f"{saving * 12:,}")
        st.caption(
            "How these numbers are calculated: saving per recommendation = current monthly cost "
            "minus the monthly cost after the change. Stop/delete and Delete orphan save 100% "
            "of the cost, Schedule shutdown saves 64% (8 PM-8 AM plus weekends), and Resize "
            "saves the price difference between the current and the new plan. Prices are "
            "illustrative (see the Assumptions tab). Each VM has only one action, so the "
            "totals do not double count.")

        st.subheader("Saving by environment")
        if accepted.empty:
            st.write("Nothing accepted yet.")
        else:
            st.bar_chart(accepted.groupby("environment")["saving"].sum())

# ================= Tab 3: Alerts & Insights =================
with tab3:
    # account-level alerts (cost jump, budget) are always shown; VM alerts follow the filters
    view_alerts = alerts[
        alerts["resource_id"].isin(filtered["resource_id"]) | (alerts["resource_id"] == "ACCOUNT")
    ].copy()

    cost_pivot = cost_hist.pivot(index="month", columns="category", values="cost")

    if view_alerts.empty:
        st.success("No alerts for the selected filters.")
    else:
        sev_options = ["Critical", "High", "Medium"]
        picked = st.multiselect("Severity", sev_options, default=sev_options)
        view_alerts = view_alerts[view_alerts["severity"].isin(picked)]

        k1, k2, k3 = st.columns(3)
        k1.metric("Critical", int((view_alerts["severity"] == "Critical").sum()))
        k2.metric("High", int((view_alerts["severity"] == "High").sum()))
        k3.metric("Medium", int((view_alerts["severity"] == "Medium").sum()))

        icons = {"Critical": "🔴", "High": "🟠", "Medium": "🟡"}
        for _, a in view_alerts.iterrows():
            label = f"{icons[a['severity']]} {a['severity']} | {a['name']} | {a['title']}"
            with st.expander(label):
                st.write("**Why this was raised:** " + a["why"])
                st.caption(f"{a['environment']} | {a['region']} | team: {a['team']}")
                if a["metric"] == "cost":
                    st.bar_chart(cost_pivot)
                else:
                    hist = metrics[metrics["resource_id"] == a["resource_id"]].set_index("timestamp")
                    st.line_chart(hist[a["metric"]])



# ================= Tab 4: What-if Simulator =================
with tab4:
    st.subheader("What-if: shut down machines during off-hours")
    st.write("Savings are counted only for machines that are actually "
             "quiet during the shutdown window, using their real 30-day usage.")

    sc1, sc2 = st.columns(2)
    sim_envs = sc1.multiselect(
        "Environments to shut down", all_envs,
        default=[e for e in ["dev", "staging"] if e in all_envs])
    include_weekends = sc2.checkbox("Also shut down all day Saturday and Sunday", value=True)

    sl1, sl2, sl3 = st.columns(3)
    start_h = sl1.slider("Shutdown starts at (hour, 24h clock)", 0, 23, 20)
    end_h = sl2.slider("Shutdown ends at (hour, 24h clock)", 0, 23, 8)
    safe_cpu = sl3.slider("Safety: max p95 CPU allowed in the window (%)", 1, 30, 5)
    allow_unsafe = st.checkbox("Also count machines that are busy in this window (risky)",
                               value=False)

    def in_window(h):
        """True if hour h falls inside the nightly shutdown window (can wrap midnight)."""
        if start_h < end_h:
            return start_h <= h < end_h
        if start_h > end_h:
            return h >= start_h or h < end_h
        return False  # same start and end = no window

    # Share of the week the machine would be off (weekday window hours + optional weekends)
    off_hours_week = 0
    for d in range(7):
        for h in range(24):
            if d >= 5:
                off_hours_week += 1 if include_weekends else (1 if in_window(h) else 0)
            else:
                off_hours_week += 1 if in_window(h) else 0
    off_fraction = off_hours_week / 168

    # Machines already recommended for Stop/delete are left out (no double counting)
    stop_ids = set(recs[recs["action"] == "Stop/delete"]["resource_id"])
    cand = filtered[filtered["environment"].isin(sim_envs)
                    & ~filtered["resource_id"].isin(stop_ids)].copy()

    if cand.empty:
        st.info("No machines match this scenario. Pick at least one environment.")
    else:
        sub = metrics[metrics["resource_id"].isin(cand["resource_id"])].copy()
        ts = pd.to_datetime(sub["timestamp"])
        weekend = ts.dt.day_of_week >= 5
        hr = ts.dt.hour
        if start_h < end_h:
            win = (hr >= start_h) & (hr < end_h)
        elif start_h > end_h:
            win = (hr >= start_h) | (hr < end_h)
        else:
            win = pd.Series(False, index=sub.index)
        off_mask = win | (weekend if include_weekends else False)
        window_p95 = sub[off_mask].groupby("resource_id")["cpu_pct"].quantile(0.95)

        cand["window_p95_cpu"] = cand["resource_id"].map(window_p95).fillna(0).round(1)
        cand["safe"] = cand["window_p95_cpu"] <= safe_cpu
        cand["counted"] = cand["safe"] | allow_unsafe
        cand["saving"] = (cand["monthly_cost"] * off_fraction * cand["counted"]).round().astype(int)

        total_saving = int(cand["saving"].sum())
        current = int(filtered["monthly_cost"].sum())

        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Hours off per week", f"{off_hours_week} hrs",
                  delta=f"{off_fraction * 100:.0f}% of the week", delta_color="off")
        r2.metric("Safe machines", f"{int(cand['safe'].sum())} of {len(cand)}")
        r3.metric("Monthly saving (Rs)", f"{total_saving:,}")
        r4.metric("Yearly saving (Rs)", f"{total_saving * 12:,}",
                  delta=f"{total_saving / current * 100:.1f}% of VM bill", delta_color="off")

        busy = int((~cand["safe"]).sum())
        if busy and not allow_unsafe:
            st.info(f"{busy} machine(s) use more than {safe_cpu}% CPU (p95) inside this window, "
                    f"so they are left out for safety. Raise the safety slider or tick the "
                    f"risky option to include them.")
        if busy and allow_unsafe:
            st.warning(f"{busy} machine(s) are busy in this window. Shutting them down "
                       f"could interrupt real work.")

        if total_saving > 0:
            st.write("**Saving by environment (Rs per month)**")
            st.bar_chart(cand.groupby("environment")["saving"].sum())

        st.write("**Machine by machine**")
        show = cand[["name", "environment", "plan", "monthly_cost",
                     "window_p95_cpu", "safe", "saving"]].sort_values("saving", ascending=False)
        st.dataframe(show, hide_index=True, use_container_width=True)

        st.caption(
            "How this is calculated: saving = monthly cost x share of the week the machine is "
            "off. A machine counts as safe if its p95 CPU during the shutdown window is at or "
            "below the safety limit. Simplifications: a stopped machine is assumed to cost "
            "nothing (in reality its disk is still billed); savings are measured against "
            "today's cost, so they overlap with the Resize recommendations and should not be "
            "added to them; machines already recommended for Stop/delete are excluded.")

    

# ================= Tab 5: Assumptions =================
with tab5:
    try:
        with open(os.path.join(BASE_DIR, "ASSUMPTIONS.md"), encoding="utf-8") as f:
            st.markdown(f.read())
    except FileNotFoundError:
        st.warning("ASSUMPTIONS.md was not found in the project folder.")

    

# ================= Tab 6: Validation =================
with tab6:
    st.subheader("Validation: are the recommendations safe and correct?")
    st.write("The data generator knows the true behaviour of every machine. The engine never "
             "sees it. Here we compare the engine's answers with that answer key, and replay "
             "real readings on the proposed smaller plans.")
    try:
        vs = pd.read_csv("validation_summary.csv")
        vr = pd.read_csv("validation_resize.csv")
        vn = pd.read_csv("validation_naive.csv")
    except FileNotFoundError:
        st.warning("Validation results not found. Run: python validation.py")
    else:
        for section in vs["section"].unique():
            st.markdown(f"**{section}**")
            st.dataframe(vs[vs["section"] == section][["check", "value", "detail"]],
                         hide_index=True, use_container_width=True)
        with st.expander("Resize backtest, machine by machine"):
            st.dataframe(vr, hide_index=True, use_container_width=True)
        with st.expander("What an average-based tool would have done"):
            st.dataframe(vn, hide_index=True, use_container_width=True)
        with st.expander("Holdout test, machine by machine"):
            st.dataframe(pd.read_csv("validation_holdout.csv"),
                         hide_index=True, use_container_width=True)
        st.caption("Limits: the data is simulated and cleaner than real data, so real "
                   "accuracy will be lower. The orphan check mostly tests the pipeline, "
                   "because orphans are given in a file rather than discovered.")

    

# ================= Tab 7: Upload Data =================
with tab7:
    st.subheader("Use your own data")
    st.write("Upload your own inventory and usage readings. The app checks the files, runs the "
             "same engine on them, and shows your results in every tab. Your files are saved in "
             "the `user_data` folder next to the app. The sample data is never changed.")

    INV_COLS = ["resource_id", "name", "region", "environment", "team", "plan",
                "vcpu", "ram_gb", "monthly_cost"]
    MET_COLS = ["resource_id", "timestamp", "cpu_pct", "mem_pct"]
    ORPH_COLS = ["resource_id", "type", "region", "size_gb", "monthly_cost", "days_unattached"]
    PLAN_COLS = ["plan", "vcpu", "ram_gb", "monthly_cost"]

    with st.expander("Required file formats"):
        st.markdown(
            "**inventory.csv** (required), one row per VM: "
            "`resource_id, name, region, environment, team, plan, vcpu, ram_gb, monthly_cost`. "
            "`environment` must be `prod`, `staging` or `dev`.\n\n"
            "**metrics.csv** (required), one row per reading: "
            "`resource_id, timestamp, cpu_pct, mem_pct`. Optional extra columns: "
            "`disk_pct, net_mbps, iops` (enable disk and network alerts). "
            "At least 7 days of data, ideally 15-minute readings.\n\n"
            "**orphans.csv** (optional): "
            "`resource_id, type, region, size_gb, monthly_cost, days_unattached`. "
            "`type` is `Volume` or `Public IP`.\n\n"
            "**plans.csv** (optional, your own plan sizes and prices): "
            "`plan, vcpu, ram_gb, monthly_cost`. Without it, resize targets and savings use "
            "the illustrative sample price list.")
        try:
            ex_inv = pd.read_csv(os.path.join(BASE_DIR, "inventory.csv"))[INV_COLS].head(5)
            ex_met = pd.read_parquet(os.path.join(BASE_DIR, "metrics.parquet"))[MET_COLS].head(5)
            d1, d2 = st.columns(2)
            d1.download_button("Download example inventory.csv", ex_inv.to_csv(index=False),
                               "example_inventory.csv", "text/csv")
            d2.download_button("Download example metrics.csv", ex_met.to_csv(index=False),
                               "example_metrics.csv", "text/csv")
        except Exception:
            pass

    if using_user_data:
        st.success("You are currently viewing your uploaded data.")
        if st.button("Switch back to the sample data"):
            st.session_state["use_user_data"] = False
            st.session_state["upload_notes"] = []
            st.cache_data.clear()
            st.rerun()

    for note in st.session_state.get("upload_notes", []):
        st.warning(note)

    up_inv = st.file_uploader("1. inventory.csv (required)", type="csv", key="up_inv")
    up_met = st.file_uploader("2. metrics.csv (required)", type="csv", key="up_met")
    up_orph = st.file_uploader("3. orphans.csv (optional)", type="csv", key="up_orph")
    up_plans = st.file_uploader("4. plans.csv (optional)", type="csv", key="up_plans")

    if st.button("Check and load my data", type="primary"):
        if up_inv is None or up_met is None:
            st.error("Please upload at least inventory.csv and metrics.csv.")
        else:
            errors, warns = [], []

            def read(file, name):
                try:
                    df = pd.read_csv(file)
                except Exception as e:
                    errors.append(f"{name}: could not be read as a CSV file ({e}).")
                    return None
                df.columns = [str(c).strip().lower() for c in df.columns]
                return df

            def missing(df, cols, name):
                miss = [c for c in cols if c not in df.columns]
                if miss:
                    errors.append(f"{name} is missing column(s): {', '.join(miss)}. "
                                  f"Columns found: {', '.join(df.columns)}.")
                return bool(miss)

            inv_u = read(up_inv, "inventory.csv")
            met_u = read(up_met, "metrics.csv")
            orph_u = read(up_orph, "orphans.csv") if up_orph is not None else None
            plans_u = read(up_plans, "plans.csv") if up_plans is not None else None

            # ---- inventory.csv ----
            if inv_u is not None and not missing(inv_u, INV_COLS, "inventory.csv"):
                for c in ["vcpu", "ram_gb", "monthly_cost"]:
                    inv_u[c] = pd.to_numeric(inv_u[c], errors="coerce")
                if inv_u[INV_COLS].isna().any().any():
                    errors.append("inventory.csv has empty cells or text where a number is "
                                  "expected (vcpu, ram_gb, monthly_cost).")
                inv_u["resource_id"] = inv_u["resource_id"].astype(str).str.strip()
                inv_u["environment"] = inv_u["environment"].astype(str).str.strip().str.lower()
                if inv_u["resource_id"].duplicated().any():
                    errors.append("inventory.csv has duplicate resource_id values.")
                bad_env = sorted(set(inv_u["environment"]) - {"prod", "staging", "dev"})
                if bad_env:
                    errors.append("inventory.csv: environment must be prod, staging or dev. "
                                  f"Found: {', '.join(bad_env)}.")
                if (inv_u[["vcpu", "ram_gb", "monthly_cost"]] <= 0).any().any():
                    errors.append("inventory.csv: vcpu, ram_gb and monthly_cost must be above 0.")

            # ---- metrics.csv ----
            if met_u is not None and not missing(met_u, MET_COLS, "metrics.csv"):
                met_u["resource_id"] = met_u["resource_id"].astype(str).str.strip()
                met_u["timestamp"] = pd.to_datetime(met_u["timestamp"], errors="coerce")
                if met_u["timestamp"].isna().all():
                    errors.append("metrics.csv: the timestamp column could not be read as dates "
                                  "(example of a good value: 2026-09-24 14:15:00).")
                else:
                    if getattr(met_u["timestamp"].dt, "tz", None) is not None:
                        met_u["timestamp"] = met_u["timestamp"].dt.tz_localize(None)
                    for c in ["cpu_pct", "mem_pct", "disk_pct", "net_mbps", "iops"]:
                        if c in met_u.columns:
                            met_u[c] = pd.to_numeric(met_u[c], errors="coerce")
                    for c in ["disk_pct", "net_mbps", "iops"]:
                        if c in met_u.columns and met_u[c].isna().any():
                            warns.append(f"metrics.csv: {c} has empty values, so it is ignored.")
                            met_u = met_u.drop(columns=[c])
                    before = len(met_u)
                    met_u = met_u.dropna(subset=["timestamp", "cpu_pct", "mem_pct"])
                    if len(met_u) < before:
                        warns.append(f"metrics.csv: {before - len(met_u)} rows with empty or "
                                     "unreadable values were dropped.")
                    pct_cols = [c for c in ["cpu_pct", "mem_pct", "disk_pct"] if c in met_u.columns]
                    out_of_range = int(sum(((met_u[c] < 0) | (met_u[c] > 100)).sum()
                                           for c in pct_cols))
                    if out_of_range:
                        warns.append(f"metrics.csv: {out_of_range} percentage values outside "
                                     "0-100 were clipped.")
                        for c in pct_cols:
                            met_u[c] = met_u[c].clip(0, 100)
                    dups = int(met_u.duplicated(["resource_id", "timestamp"]).sum())
                    if dups:
                        warns.append(f"metrics.csv: {dups} duplicate readings were dropped.")
                        met_u = met_u.drop_duplicates(["resource_id", "timestamp"])

            # ---- orphans.csv (optional) ----
            if orph_u is not None and not missing(
                    orph_u, ["resource_id", "type", "region", "monthly_cost", "days_unattached"],
                    "orphans.csv"):
                if "size_gb" not in orph_u.columns:
                    orph_u["size_gb"] = 0
                for c in ["size_gb", "monthly_cost", "days_unattached"]:
                    orph_u[c] = pd.to_numeric(orph_u[c], errors="coerce")
                if orph_u[ORPH_COLS].isna().any().any():
                    errors.append("orphans.csv has empty cells or text where a number is expected.")
                kinds = {"volume": "Volume", "public ip": "Public IP", "ip": "Public IP"}
                orph_u["type"] = orph_u["type"].astype(str).str.strip().str.lower().map(kinds)
                if orph_u["type"].isna().any():
                    errors.append("orphans.csv: type must be Volume or Public IP.")
                orph_u = orph_u[ORPH_COLS]

            # ---- plans.csv (optional) ----
            if plans_u is not None and not missing(plans_u, PLAN_COLS, "plans.csv"):
                for c in ["vcpu", "ram_gb", "monthly_cost"]:
                    plans_u[c] = pd.to_numeric(plans_u[c], errors="coerce")
                if plans_u[PLAN_COLS].isna().any().any() or (
                        plans_u[["vcpu", "ram_gb", "monthly_cost"]] <= 0).any().any():
                    errors.append("plans.csv: vcpu, ram_gb and monthly_cost must be numbers above 0.")
                if plans_u["plan"].duplicated().any():
                    errors.append("plans.csv has duplicate plan names.")
                plans_u = plans_u[PLAN_COLS]

            # ---- checks that need both files ----
            if not errors:
                ids_inv, ids_met = set(inv_u["resource_id"]), set(met_u["resource_id"])
                no_data = ids_inv - ids_met
                unknown = ids_met - ids_inv
                if no_data:
                    warns.append(f"{len(no_data)} VM(s) in inventory.csv have no readings and were "
                                 "left out.")
                    inv_u = inv_u[~inv_u["resource_id"].isin(no_data)]
                if unknown:
                    warns.append(f"{len(unknown)} resource_id(s) in metrics.csv are not in "
                                 "inventory.csv and were ignored.")
                    met_u = met_u[met_u["resource_id"].isin(ids_inv)]
                if inv_u.empty or met_u.empty:
                    errors.append("No VM has both an inventory row and readings.")
                else:
                    span = (met_u["timestamp"].max() - met_u["timestamp"].min()).days + 1
                    if span < 7:
                        errors.append(f"metrics.csv covers only {span} day(s). At least 7 days "
                                      "are needed.")
                    gap = (met_u.sort_values(["resource_id", "timestamp"])
                           .groupby("resource_id")["timestamp"].diff().median())
                    if pd.notna(gap) and abs(gap.total_seconds() / 60 - 15) > 1:
                        warns.append(f"Readings are about {gap.total_seconds() / 60:.0f} minutes "
                                     "apart. The alert thresholds are tuned for 15-minute "
                                     "readings, so spike alerts may be less sensitive.")

            if errors:
                st.error("Your data was not loaded. Fix these problems and try again:")
                for e in errors:
                    st.write("- " + e)
            else:
                # ---- save into user_data and run the same scripts there ----
                os.makedirs(USER_DIR, exist_ok=True)
                for f in os.listdir(USER_DIR):
                    if f.endswith((".csv", ".parquet")):
                        os.remove(os.path.join(USER_DIR, f))
                inv_u.to_csv(os.path.join(USER_DIR, "inventory.csv"), index=False)
                met_u.to_parquet(os.path.join(USER_DIR, "metrics.parquet"), index=False)
                if orph_u is None:
                    orph_u = pd.DataFrame(columns=ORPH_COLS)
                orph_u.to_csv(os.path.join(USER_DIR, "orphans.csv"), index=False)
                if plans_u is not None:
                    plans_u.to_csv(os.path.join(USER_DIR, "plans.csv"), index=False)

                failed = None
                with st.spinner("Running the engine, cost history, alerts and validation..."):
                    for script in ["engine.py", "cost_history.py", "alerts.py", "validation.py"]:
                        res = subprocess.run([sys.executable, os.path.join(BASE_DIR, script)],
                                             cwd=USER_DIR, capture_output=True, text=True)
                        if res.returncode != 0:
                            if script == "validation.py":
                                warns.append("The Validation tab could not be built for this data.")
                            else:
                                failed = (script, res.stderr[-1500:])
                                break
                if failed:
                    st.error(f"{failed[0]} stopped with an error on your data:")
                    st.code(failed[1])
                else:
                    warns.append("The monthly cost history is simulated: the latest month is your "
                                 "current bill and earlier months are random. Upload-based "
                                 "recommendations and alerts use your real readings.")
                    st.session_state["upload_notes"] = warns
                    st.session_state["use_user_data"] = True
                    st.cache_data.clear()
                    st.rerun()
        
    # ----- Cost history -----
    st.divider()
    st.subheader("Why did the cloud bill grow over the last 3 months?")
    totals = cost_pivot.sum(axis=1)
    h1, h2, h3 = st.columns(3)
    h1.metric("First month (Rs)", f"{int(totals.iloc[0]):,}")
    h2.metric("Latest month (Rs)", f"{int(totals.iloc[-1]):,}",
              delta=f"+{(totals.iloc[-1] / totals.iloc[0] - 1) * 100:.0f}%", delta_color="inverse")
    h3.metric("Approved monthly budget (Rs)", f"{budget:,}")
    st.bar_chart(cost_pivot)
    st.write("**What caused the growth** (illustrative breakdown, see assumptions):")
    st.dataframe(drivers, hide_index=True, use_container_width=True)