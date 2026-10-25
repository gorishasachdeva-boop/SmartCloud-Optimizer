import os
import pandas as pd

# Same planning limits as engine.py (also listed in ASSUMPTIONS.md)
CPU_LIMIT = 70
MEM_LIMIT = 80
SAT_LIMIT = 100   # 100% = completely out of capacity

inv = pd.read_csv("inventory.csv")
for _c in ["anomaly", "archetype"]:   # only our simulated data has these answer keys
    if _c not in inv.columns:
        inv[_c] = ""
inv["anomaly"] = inv["anomaly"].fillna("")
inv["archetype"] = inv["archetype"].fillna("")
has_labels = bool((inv["archetype"].astype(str) != "").any())
met = pd.read_parquet("metrics.parquet")
recs = pd.read_csv("recommendations.csv")
orph = pd.read_csv("orphans.csv")
alerts = pd.read_csv("alerts.csv")

# Plan sizes and prices, taken from the inventory so nothing is duplicated
# Plan sizes and prices: the user's plans.csv if there is one, otherwise the sample price list
SAMPLE_PLANS = {"S1": (1, 2, 700), "S2": (2, 4, 1400), "S4": (4, 8, 2800),
                "S8": (8, 16, 5600), "S16": (16, 32, 11200)}
if os.path.exists("plans.csv"):
    plans = (pd.read_csv("plans.csv").set_index("plan")[["vcpu", "ram_gb", "monthly_cost"]]
             .sort_values(["vcpu", "monthly_cost"]))
else:
    plans = (pd.DataFrame.from_dict(SAMPLE_PLANS, orient="index",
                                    columns=["vcpu", "ram_gb", "monthly_cost"])
             .sort_values("vcpu"))
series = {rid: grp for rid, grp in met.groupby("resource_id")}

summary = []


def add(section, check, value, detail):
    summary.append({"section": section, "check": check, "value": str(value), "detail": detail})


def prf(truth, pred):
    """precision and recall for two sets of ids"""
    tp, fp, fn = len(truth & pred), len(pred - truth), len(truth - pred)
    precision = f"{tp / (tp + fp) * 100:.0f}%" if (tp + fp) else "n/a"
    recall = f"{tp / (tp + fn) * 100:.0f}%" if (tp + fn) else "n/a"
    return tp, fp, fn, precision, recall


# ---------- 1. Resize backtest ----------
RZ_COLS = ["resource_id", "name", "environment", "from_plan", "to_plan",
           "cpu_p99_after", "cpu_max_after", "mem_p99_after", "mem_max_after",
           "p99_within_limits", "never_saturates", "archetype"]
rs = recs[recs["action"] == "Resize"].merge(
    inv[["resource_id", "vcpu", "ram_gb", "archetype"]], on="resource_id")
rows = []
for _, r in rs.iterrows():
    if r["target_plan"] not in plans.index:
        continue
    t = plans.loc[r["target_plan"]]
    m = series[r["resource_id"]]
    cpu = m["cpu_pct"] * r["vcpu"] / t["vcpu"]      # the same readings on the smaller plan
    mem = m["mem_pct"] * r["ram_gb"] / t["ram_gb"]
    rows.append({
        "resource_id": r["resource_id"], "name": r["name"], "environment": r["environment"],
        "from_plan": r["plan"], "to_plan": r["target_plan"],
        "cpu_p99_after": round(cpu.quantile(0.99), 1), "cpu_max_after": round(cpu.max(), 1),
        "mem_p99_after": round(mem.quantile(0.99), 1), "mem_max_after": round(mem.max(), 1),
        "p99_within_limits": bool(cpu.quantile(0.99) <= CPU_LIMIT and mem.quantile(0.99) <= MEM_LIMIT),
        "never_saturates": bool(cpu.max() < SAT_LIMIT and mem.max() < SAT_LIMIT),
        "archetype": r["archetype"],   # hidden label, for checking only
    })
rz = pd.DataFrame(rows, columns=RZ_COLS)
rz.to_csv("validation_resize.csv", index=False)

n = len(rz)
sec = "1. Resize safety (30-day replay on the new plan)"
add(sec, "Resize recommendations tested", n, "Every resize is replayed on its new, smaller plan.")
add(sec, "Never ran out of capacity (max below 100%)",
    f"{int(rz['never_saturates'].sum())} of {n}",
    "No single reading, including the daily peak, would have hit 100% CPU or memory.")
add(sec, "99th percentile within planning limits",
    f"{int(rz['p99_within_limits'].sum())} of {n}",
    f"p99 CPU at or below {CPU_LIMIT}% and p99 memory at or below {MEM_LIMIT}% after the resize. "
    "A fail means the planning limit would be passed by the rarest peaks. See the table below.")
add(sec, "Bursty or batch VMs that were resized",
    int(rs["archetype"].isin(["bursty", "batch"]).sum()),
    "Should be 0. These VMs look idle on average but peak hard, so shrinking them is unsafe.")

# ---------- 2. What an average-based tool would do ----------
NV_COLS = ["resource_id", "name", "environment", "from_plan", "average_based_plan",
           "claimed_saving_rs", "cpu_p99_after", "would_break", "archetype"]
pstats = pd.DataFrame({
    "cpu_mean": met.groupby("resource_id")["cpu_pct"].mean(),
    "mem_mean": met.groupby("resource_id")["mem_pct"].mean(),
    "cpu_p99": met.groupby("resource_id")["cpu_pct"].quantile(0.99),
    "mem_p99": met.groupby("resource_id")["mem_pct"].quantile(0.99),
})
nrows = []
for _, v in inv.iterrows():
    s = pstats.loc[v["resource_id"]]
    for pname, p in plans.iterrows():
        if (s["cpu_mean"] * v["vcpu"] / p["vcpu"] <= CPU_LIMIT
                and s["mem_mean"] * v["ram_gb"] / p["ram_gb"] <= MEM_LIMIT):
            if p["monthly_cost"] < v["monthly_cost"]:
                cpu99 = s["cpu_p99"] * v["vcpu"] / p["vcpu"]
                mem99 = s["mem_p99"] * v["ram_gb"] / p["ram_gb"]
                nrows.append({
                    "resource_id": v["resource_id"], "name": v["name"],
                    "environment": v["environment"], "from_plan": v["plan"],
                    "average_based_plan": pname,
                    "claimed_saving_rs": int(v["monthly_cost"] - p["monthly_cost"]),
                    "cpu_p99_after": round(cpu99, 1),
                    "would_break": bool(cpu99 > SAT_LIMIT or mem99 > SAT_LIMIT),
                    "archetype": v["archetype"],
                })
            break   # smallest fitting plan found
nv = pd.DataFrame(nrows, columns=NV_COLS)
nv.to_csv("validation_naive.csv", index=False)

broken = nv[nv["would_break"]]
sec = "2. Average-based sizing vs this engine"
add(sec, "Resizes an average-based tool would make", len(nv),
    "Sized on average CPU and memory only, with the same 70% and 80% limits.")
add(sec, "...of which would run out of capacity at the daily peak", len(broken),
    "At least 1% of readings would be above 100% on the smaller plan.")
add(sec, "Monthly saving those broken resizes would claim (Rs)",
    f"{int(broken['claimed_saving_rs'].sum()):,}", "Savings that would come with outages.")
if has_labels:
    add(sec, "Types of VM it would break",
        ", ".join(f"{k}: {v}" for k, v in broken["archetype"].value_counts().items()) or "none",
        "Hidden labels, shown only to explain the result.")
add(sec, "This engine: resizes that run out of capacity", n - int(rz["never_saturates"].sum()),
    "Same test, using p99 sizing for prod and bursty VMs.")

# ---------- 3. Precision and recall against the hidden labels ----------
sec = "3. Detection accuracy (against hidden labels)"
checks = [
    ("Idle VMs: Stop/delete",
     set(inv[inv["archetype"] == "idle"]["resource_id"]),
     set(recs[recs["action"] == "Stop/delete"]["resource_id"]),
     "Truth: VMs generated as idle (about 1% CPU)."),
    ("Dev/staging office-hours VMs: Schedule shutdown",
     set(inv[inv["archetype"] == "office_hours"]["resource_id"]),
     set(recs[recs["action"] == "Schedule shutdown"]["resource_id"]),
     "Truth: VMs generated as busy only on weekdays 9 to 7."),
    ("Orphans: Delete orphan",
     set(orph["resource_id"]),
     set(recs[recs["action"] == "Delete orphan"]["resource_id"]),
     "Truth: every row in orphans.csv. This mostly checks the pipeline, since orphans are "
     "listed in a file rather than discovered."),
]
if has_labels:
    for label, truth, pred, note in checks:
        tp, fp, fn, p, r = prf(truth, pred)
        add(sec, label, f"precision {p}, recall {r}",
            f"{tp} correct, {fp} wrongly flagged, {fn} missed. {note}")
else:
    add(sec, "Detection accuracy", "skipped",
        "Uploaded data has no hidden answer key, so precision and recall cannot be measured.")

# ---------- 4. Alerts ----------
sec = "4. Alerts"
want_type = {"net_spike": "usage_spike", "disk_fill": "capacity_trend"}
planted = inv[inv["anomaly"] != ""]
vm_alerts = alerts[alerts["alert_type"].isin(["usage_spike", "capacity_trend"])]
found = 0
for _, p in planted.iterrows():
    hit = ((vm_alerts["resource_id"] == p["resource_id"])
           & (vm_alerts["alert_type"] == want_type[p["anomaly"]])).any()
    found += int(hit)
false_alarms = len(vm_alerts) - found
if len(planted) > 0:
    add(sec, "Planted anomalies found", f"{found} of {len(planted)}",
        "A 3-day network spike and a disk filling 2 points per day.")
    add(sec, "False alarms on VMs (daily peaks, batch jobs)", false_alarms,
        "Alerts on machines with no planted problem. Bursty and batch VMs should not raise any.")
else:
    add(sec, "VM alerts raised", len(vm_alerts),
        "No planted anomalies in this data, so false alarms cannot be measured. "
        "Read each alert's explanation to judge it.")

# ---------- 5. Holdout test: learn on 21 days, test on the 9 days it never saw ----------
met["timestamp"] = pd.to_datetime(met["timestamp"])
span_days = (met["timestamp"].max() - met["timestamp"].min()).days + 1
TRAIN_DAYS = max(1, min(21, int(span_days * 0.7)))   # shrinks for short data
cut = met["timestamp"].min() + pd.Timedelta(days=TRAIN_DAYS)
train_by = {rid: g for rid, g in met[met["timestamp"] < cut].groupby("resource_id")}
test_by = {rid: g for rid, g in met[met["timestamp"] >= cut].groupby("resource_id")}

HO_COLS = ["resource_id", "name", "environment", "plan_chosen_on_21_days",
           "plan_chosen_on_30_days", "unseen_cpu_p99", "unseen_cpu_max",
           "unseen_mem_max", "safe_on_unseen_days"]
hrows = []
for _, r in rs.iterrows():
    if r["resource_id"] not in train_by or r["resource_id"] not in test_by:
        continue
    a = train_by[r["resource_id"]]
    mean_cpu = a["cpu_pct"].mean()
    bursty = a["cpu_pct"].quantile(0.99) > 5 * mean_cpu          # same rule as engine.py
    q = 0.99 if (r["environment"] == "prod" or bursty) else 0.95  # same rule as engine.py
    used_vcpu = a["cpu_pct"].quantile(q) / 100 * r["vcpu"]
    used_ram = a["mem_pct"].quantile(q) / 100 * r["ram_gb"]

    chosen = r["plan"]                      # if nothing fits, keep the current plan
    for pname, p in plans.iterrows():       # plans are ordered small to large
        if (used_vcpu / p["vcpu"] * 100 <= CPU_LIMIT
                and used_ram / p["ram_gb"] * 100 <= MEM_LIMIT):
            chosen = pname
            break

    t = plans.loc[chosen]
    b = test_by[r["resource_id"]]
    cpu = b["cpu_pct"] * r["vcpu"] / t["vcpu"]
    mem = b["mem_pct"] * r["ram_gb"] / t["ram_gb"]
    hrows.append({
        "resource_id": r["resource_id"], "name": r["name"], "environment": r["environment"],
        "plan_chosen_on_21_days": chosen, "plan_chosen_on_30_days": r["target_plan"],
        "unseen_cpu_p99": round(cpu.quantile(0.99), 1),
        "unseen_cpu_max": round(cpu.max(), 1), "unseen_mem_max": round(mem.max(), 1),
        "safe_on_unseen_days": bool(cpu.max() < SAT_LIMIT and mem.max() < SAT_LIMIT),
    })
ho = pd.DataFrame(hrows, columns=HO_COLS)
ho.to_csv("validation_holdout.csv", index=False)

sec = "5. Holdout test (learn on 21 days, test on the next 9)"
add(sec, "Resizes tested", len(ho),
    "The sizing rule is re-run using only the first 21 days, then that plan is replayed "
    "on the last 9 days, which it never saw.")
add(sec, "Safe on the unseen 9 days (no reading at 100%)",
    f"{int(ho['safe_on_unseen_days'].sum())} of {len(ho)}",
    "No CPU or memory reading on those days would have hit 100% on the chosen plan.")
add(sec, "Same plan chosen with 21 days as with 30 days",
    f"{int((ho['plan_chosen_on_21_days'] == ho['plan_chosen_on_30_days']).sum())} of {len(ho)}",
    "Shows the recommendation is stable and does not depend on a few extra days.")

# ---------- Save and print ----------
out = pd.DataFrame(summary)
out.to_csv("validation_summary.csv", index=False)
print(out[["section", "check", "value"]].to_string(index=False))