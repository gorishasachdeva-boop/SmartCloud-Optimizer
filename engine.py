import pandas as pd

# ---------- Settings (copy these into ASSUMPTIONS.md) ----------
PLANS = {  # must match generate_data.py, ordered small -> large
    "S1":  {"vcpu": 1,  "ram": 2,  "cost": 700},
    "S2":  {"vcpu": 2,  "ram": 4,  "cost": 1400},
    "S4":  {"vcpu": 4,  "ram": 8,  "cost": 2800},
    "S8":  {"vcpu": 8,  "ram": 16, "cost": 5600},
    "S16": {"vcpu": 16, "ram": 32, "cost": 11200},
}

import os
if os.path.exists("plans.csv"):   # optional: the user's own plan prices
    _pl = pd.read_csv("plans.csv").sort_values(["vcpu", "monthly_cost"])
    PLANS = {r["plan"]: {"vcpu": int(r["vcpu"]), "ram": float(r["ram_gb"]),
                         "cost": float(r["monthly_cost"])} for _, r in _pl.iterrows()}
    
CPU_TARGET = 70        # after resize, peak CPU must stay <= 70%
MEM_TARGET = 80        # after resize, peak memory must stay <= 80%
IDLE_P99 = 5           # p99 CPU under 5% = idle
OFFHOURS_P95 = 5       # off-hours p95 CPU under 5% = safe to schedule
OFFHOURS_SAVING = 0.64 # shut down 8PM-8AM + weekends = 108 of 168 hours
BURSTY_RATIO = 5       # p99 more than 5x the average = bursty

inventory = pd.read_csv("inventory.csv")
metrics = pd.read_parquet("metrics.parquet")
metrics["timestamp"] = pd.to_datetime(metrics["timestamp"])

# Off-hours = anything outside weekdays 8 AM to 8 PM
hour = metrics["timestamp"].dt.hour
weekday = metrics["timestamp"].dt.dayofweek < 5
metrics["off_hours"] = ~(weekday & (hour >= 8) & (hour < 20))

# ---------- Per-machine statistics ----------
g = metrics.groupby("resource_id")
stats = pd.DataFrame({
    "cpu_mean": g["cpu_pct"].mean(),
    "cpu_p95": g["cpu_pct"].quantile(0.95),
    "cpu_p99": g["cpu_pct"].quantile(0.99),
    "mem_p95": g["mem_pct"].quantile(0.95),
    "mem_p99": g["mem_pct"].quantile(0.99),
})
stats["off_p95"] = metrics[metrics["off_hours"]].groupby("resource_id")["cpu_pct"].quantile(0.95)
stats["on_mean"] = metrics[~metrics["off_hours"]].groupby("resource_id")["cpu_pct"].mean()

df = inventory.merge(stats, left_on="resource_id", right_index=True)

# ---------- Decide an action for each machine ----------
recs = []
for _, vm in df.iterrows():
    cost = int(vm["monthly_cost"])
    prod = vm["environment"] == "prod"
    bursty = vm["cpu_p99"] > BURSTY_RATIO * vm["cpu_mean"]

    action, target, new_cost, risk, reason = None, "", cost, "", ""

    # Rule 1: idle
    if vm["cpu_p99"] < IDLE_P99:
        action = "Stop/delete"
        new_cost = 0
        risk = "Medium" if prod else "Low"
        reason = (f"Idle: p99 CPU is only {vm['cpu_p99']:.1f}% "
                  f"(average {vm['cpu_mean']:.1f}%) for the whole 30 days.")

    # Rule 2: schedule shutdown (non-prod only)
    elif (not prod) and vm["off_p95"] < OFFHOURS_P95 and vm["on_mean"] > 15:
        action = "Schedule shutdown"
        new_cost = round(cost * (1 - OFFHOURS_SAVING))
        risk = "Low"
        reason = (f"Used in working hours (avg {vm['on_mean']:.0f}% CPU) but near zero "
                  f"nights and weekends (p95 {vm['off_p95']:.1f}%). "
                  f"Shut down 8 PM-8 AM and weekends.")

    # Rule 3: rightsize
    else:
        use_p99 = prod or bursty  # the bursty guard: size on the peak
        cpu_peak = vm["cpu_p99"] if use_p99 else vm["cpu_p95"]
        mem_peak = vm["mem_p99"] if use_p99 else vm["mem_p95"]
        used_vcpu = cpu_peak / 100 * vm["vcpu"]
        used_ram = mem_peak / 100 * vm["ram_gb"]

        for name, p in PLANS.items():
            new_cpu = used_vcpu / p["vcpu"] * 100
            new_mem = used_ram / p["ram"] * 100
            if new_cpu <= CPU_TARGET and new_mem <= MEM_TARGET:
                if p["cost"] < cost:
                    action = "Resize"
                    target = name
                    new_cost = p["cost"]
                    if new_cpu > 65:
                        risk = "High"
                    elif new_cpu > 50 or bursty:
                        risk = "Medium"
                    else:
                        risk = "Low"
                    which = "p99" if use_p99 else "p95"
                    reason = (f"Over-provisioned: {which} CPU is {cpu_peak:.0f}% "
                              f"(avg {vm['cpu_mean']:.0f}%) on {vm['plan']}. "
                              f"On {name}, peak CPU would be {new_cpu:.0f}% and "
                              f"memory {new_mem:.0f}%.")
                break  # smallest fitting plan found

    if action:
        recs.append({
            "resource_id": vm["resource_id"], "name": vm["name"],
            "region": vm["region"], "environment": vm["environment"],
            "team": vm["team"], "plan": vm["plan"],
            "action": action, "target_plan": target,
            "current_cost": cost, "new_cost": new_cost,
            "saving": cost - new_cost, "risk": risk, "reason": reason,
        })

# ---------- Orphans: unattached volumes and unused public IPs ----------
ORPHAN_MIN_DAYS = 30   # only delete if unattached for 30+ days

orph = pd.read_csv("orphans.csv")
needed = {"resource_id", "type", "region", "monthly_cost", "days_unattached"}
if not needed.issubset(orph.columns):
    raise SystemExit("orphans.csv is missing expected columns. Found: "
                     + ", ".join(orph.columns))

for _, o in orph.iterrows():
    if o["days_unattached"] < ORPHAN_MIN_DAYS:
        continue
    cost = int(o["monthly_cost"])
    if o["type"] == "Volume":
        what = f"Unattached {int(o['size_gb'])} GB volume"
        tip = " Take a snapshot first if you are unsure it holds needed data."
    else:
        what = "Unused public IP"
        tip = ""
    recs.append({
        "resource_id": o["resource_id"], "name": o["resource_id"],
        "region": o["region"], "environment": "orphan",
        "team": "unassigned", "plan": o["type"],
        "action": "Delete orphan", "target_plan": "",
        "current_cost": cost, "new_cost": 0, "saving": cost,
        "risk": "Low",
        "reason": (f"{what} has not been attached to any machine for "
                   f"{int(o['days_unattached'])} days but still costs Rs {cost:,} per month "
                   f"(rule: delete after {ORPHAN_MIN_DAYS}+ days).{tip}"),
    })

recs_df = pd.DataFrame(recs, columns=[
    "resource_id", "name", "region", "environment", "team", "plan", "action",
    "target_plan", "current_cost", "new_cost", "saving", "risk", "reason"])
recs_df.to_csv("recommendations.csv", index=False)

# ---------- Summary + safety check ----------
print("Recommendations:", len(recs_df), "of", len(df), "machines")
print(recs_df["action"].value_counts().to_string())
print("Total monthly saving: Rs", recs_df["saving"].sum(),
      "of Rs", df["monthly_cost"].sum() + orph["monthly_cost"].sum())

if "archetype" in df.columns:   # only our simulated data has the hidden answer key
    print("\nSafety check (uses the hidden answer key):")
    check = df[["resource_id", "archetype"]].merge(
        recs_df[["resource_id", "action"]], on="resource_id", how="left").fillna({"action": "Keep"})
    print(check.groupby(["archetype", "action"]).size().to_string())