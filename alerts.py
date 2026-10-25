import numpy as np
import pandas as pd

# ---------- Thresholds (all listed in ASSUMPTIONS.md later) ----------
BASELINE_DAYS = 21     # first 21 days = "what normal looks like"
Z_THRESHOLD = 5        # how far from normal counts as unusual
ROLL = 12              # smooth over 12 readings (3 hours) so one noisy reading can't fire
MIN_RUN = 16           # unusual for at least 16 readings (4 hours) to raise an alert
TREND_DAYS = 14        # capacity trend uses the last 14 days
MIN_SLOPE = 0.1        # ignore growth slower than 0.1 percentage points per day
MIN_R2 = 0.6           # trend line must fit the data well (confidence check)
FULL_WARN_DAYS = 30    # warn if full within 30 days
FULL_CRIT_DAYS = 10    # critical if full within 10 days

inv = pd.read_csv("inventory.csv")
if "anomaly" not in inv.columns:   # only our simulated data has planted anomalies
    inv["anomaly"] = ""
met = pd.read_parquet("metrics.parquet")
met["timestamp"] = pd.to_datetime(met["timestamp"])
met = met.sort_values(["resource_id", "timestamp"]).reset_index(drop=True)

start = met["timestamp"].min()
span_days = (met["timestamp"].max() - met["timestamp"].min()).days + 1
BASELINE_DAYS = max(1, min(BASELINE_DAYS, int(span_days * 0.7)))
split = start + pd.Timedelta(days=BASELINE_DAYS)
met["slot"] = met["timestamp"].dt.hour * 4 + met["timestamp"].dt.minute // 15
met["weekend"] = met["timestamp"].dt.day_of_week >= 5

alerts = []


# ---------- 1. Usage spikes ----------
def find_spikes(metric, label, unit):
    """Compare each reading with the SAME time of day (weekday/weekend separately)
    during the baseline period. Daily peaks are normal, so they are not flagged."""
    df = met[["resource_id", "timestamp", "slot", "weekend", metric]].copy()
    keys = ["resource_id", "weekend", "slot"]

    base = df[df["timestamp"] < split].copy()
    base["med"] = base.groupby(keys)[metric].transform("median")
    base["dev"] = (base[metric] - base["med"]).abs()
    stats = base.groupby(keys).agg(med=("med", "first"), mad=("dev", "median")).reset_index()

    df = df.merge(stats, on=keys, how="left")
    df = df.sort_values(["resource_id", "timestamp"]).reset_index(drop=True)

    # "typical variation" with a floor, so quiet machines don't over-trigger
    scale = np.maximum(np.maximum(df["mad"] * 1.4826, df["med"] * 0.1), 1.0)
    df["z"] = (df[metric] - df["med"]) / scale
    df["zroll"] = df.groupby("resource_id")["z"].transform(
        lambda s: s.rolling(ROLL, min_periods=ROLL).median()
    )
    df["flag"] = (df["zroll"] > Z_THRESHOLD) & (df["timestamp"] >= split)
    df["run"] = (df["flag"] != df.groupby("resource_id")["flag"].shift()).cumsum()

    for _, run in df[df["flag"]].groupby("run"):
        if len(run) < MIN_RUN:
            continue
        observed = run[metric].mean()
        normal = run["med"].mean()
        ratio = observed / max(normal, 1.0)
        hours = len(run) * 0.25
        t0, t1 = run["timestamp"].iloc[0], run["timestamp"].iloc[-1]
        if ratio >= 8:
            sev = "Critical"
        elif ratio >= 4:
            sev = "High"
        else:
            sev = "Medium"
        why = (
            f"{label} averaged {observed:.0f} {unit} for {hours:.0f} hours "
            f"(from {t0:%d %b %H:%M} to {t1:%d %b %H:%M}). At this time of day it is "
            f"normally around {normal:.0f} {unit}, so this is {ratio:.1f}x higher. "
            f"Raised because the reading stayed more than {Z_THRESHOLD} 'typical variations' "
            f"above normal for at least {MIN_RUN * 15 // 60} hours. Normal is learned from "
            f"the first {BASELINE_DAYS} days, so repeating daily peaks are not flagged."
        )
        alerts.append({
            "resource_id": run["resource_id"].iloc[0],
            "alert_type": "usage_spike",
            "metric": metric,
            "severity": sev,
            "title": f"Unusual {label} spike ({ratio:.1f}x normal)",
            "why": why,
            "start": t0, "end": t1,
        })


# ---------- 2. Capacity trends ("days until full") ----------
def find_capacity(metric, label):
    last_day = met["timestamp"].max().floor("D")
    cutoff = last_day - pd.Timedelta(days=TREND_DAYS - 1)
    recent = met[met["timestamp"] >= cutoff]
    daily = (recent.groupby(["resource_id", recent["timestamp"].dt.floor("D")])[metric]
             .mean().reset_index())

    for rid, g in daily.groupby("resource_id"):
        if len(g) < 5:   # too little data for a trend
            continue
        x = np.arange(len(g))
        x = np.arange(len(g))
        y = g[metric].values
        slope, intercept = np.polyfit(x, y, 1)      # straight-line fit
        if slope <= MIN_SLOPE:
            continue
        pred = slope * x + intercept
        ss_res = ((y - pred) ** 2).sum()
        ss_tot = ((y - y.mean()) ** 2).sum()
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        if r2 < MIN_R2:
            continue
        current = y[-1]
        days_left = (100 - current) / slope
        if days_left > FULL_WARN_DAYS:
            continue
        sev = "Critical" if days_left <= FULL_CRIT_DAYS else "High"
        why = (
            f"{label} grew by {slope:.1f} percentage points per day over the last "
            f"{TREND_DAYS} days (straight-line fit quality R2 = {r2:.2f}, "
            f"where 1.00 is a perfect fit). It is now {current:.0f}% full. If the trend "
            f"continues it reaches 100% in about {days_left:.0f} days. Raised because "
            f"the time to full is under {FULL_WARN_DAYS} days."
        )
        alerts.append({
            "resource_id": rid,
            "alert_type": "capacity_trend",
            "metric": metric,
            "severity": sev,
            "title": f"{label} full in about {days_left:.0f} days",
            "why": why,
            "start": g["timestamp"].iloc[0], "end": g["timestamp"].iloc[-1],
        })


if "net_mbps" in met.columns:   # skip checks for columns the user did not provide
    find_spikes("net_mbps", "Network traffic", "Mbps")
find_spikes("cpu_pct", "CPU", "%")
if "disk_pct" in met.columns:
    find_capacity("disk_pct", "Disk")
find_capacity("mem_pct", "Memory")

# ---------- 3. Cost jumps and 4. Budget overrun (account level) ----------
COST_JUMP_FACTOR = 1.5   # a month-on-month rise bigger than 1.5x the typical rise is a "jump"
FORECAST_MONTHS = 3      # look 3 months ahead

ch = pd.read_csv("cost_history.csv").fillna("")
budget = float(pd.read_csv("budget.csv")["monthly_budget"].iloc[0])
months = list(dict.fromkeys(ch["month"]))
totals = ch.groupby("month")["cost"].sum().reindex(months)

# Cost jumps: which month rose much faster than usual, and who caused it?
steps = totals.diff().dropna()
typical = steps.min()
for m, inc in steps.items():
    if inc <= COST_JUMP_FACTOR * typical:
        continue
    prev = months[months.index(m) - 1]
    now_c = ch[ch["month"] == m].set_index("category")["cost"]
    old_c = ch[ch["month"] == prev].set_index("category")["cost"]
    diff = (now_c - old_c).sort_values(ascending=False)
    parts = ", ".join(f"{c} +Rs {v:,.0f} ({v / inc * 100:.0f}%)" for c, v in diff.items() if v > 0)
    events = " ".join(n + "." for n in ch[ch["month"] == m]["note"] if n)
    pct = inc / totals[prev] * 100
    month_name = pd.to_datetime(m, format="%Y-%m")
    why = (
        f"The bill rose by Rs {inc:,.0f} ({pct:.0f}%) from {pd.to_datetime(prev, format='%Y-%m'):%b} "
        f"to {month_name:%b}. A normal month-on-month rise is about Rs {typical:,.0f}, so this "
        f"is {inc / typical:.1f}x larger. Who caused it: {parts}. {events} "
        f"Raised because the rise was more than {COST_JUMP_FACTOR}x the typical monthly rise."
    )
    alerts.append({
        "resource_id": "ACCOUNT", "alert_type": "cost_jump", "metric": "cost",
        "severity": "High" if pct >= 8 else "Medium",
        "title": f"Cost jumped {pct:.0f}% in {month_name:%b %Y} (+Rs {inc:,.0f})",
        "why": why, "start": month_name, "end": month_name,
    })

# Budget overrun: straight-line trend over all months, projected forward
x = np.arange(len(totals))
slope, intercept = np.polyfit(x, totals.values, 1)
future = [intercept + slope * (len(totals) - 1 + k) for k in range(1, FORECAST_MONTHS + 1)]
current = totals.iloc[-1]
last_month = pd.to_datetime(months[-1], format="%Y-%m")
if current > budget or max(future) > budget:
    final_month = last_month + pd.DateOffset(months=FORECAST_MONTHS)
    if current > budget:
        sev = "Critical"
        title = f"Already Rs {current - budget:,.0f} per month over budget"
        extra = (f"Current spend is {((current / budget) - 1) * 100:.0f}% above budget. "
                 f"If nothing changes that is about Rs {(current - budget) * 12:,.0f} over budget in a year.")
    else:
        sev = "High"
        first = next(k for k, f in enumerate(future, 1) if f > budget)
        title = f"Projected to exceed budget in {first} month(s)"
        extra = "Spend is currently under budget but the trend will cross it."
    why = (
        f"The approved monthly budget is Rs {budget:,.0f} (assumption: the first month's cost plus 20%). "
        f"Spend in {last_month:%b %Y} is Rs {current:,.0f}. Over the last {len(totals)} months "
        f"the bill grew by about Rs {slope:,.0f} per month (straight-line trend). If that continues, "
        f"spend reaches about Rs {future[-1]:,.0f} per month by {final_month:%b %Y}. {extra} "
        f"Raised because current or projected spend is above the budget."
    )
    alerts.append({
        "resource_id": "ACCOUNT", "alert_type": "budget_overrun", "metric": "cost",
        "severity": sev, "title": title, "why": why,
        "start": last_month, "end": final_month,
    })

# ---------- Save ----------
cols = ["alert_id", "resource_id", "name", "environment", "region", "team",
        "alert_type", "metric", "severity", "title", "why", "start", "end"]
if alerts:
    out = pd.DataFrame(alerts).merge(
        inv[["resource_id", "name", "environment", "region", "team"]],
        on="resource_id", how="left")
    out["name"] = out["name"].fillna("Whole account")
    for c in ["environment", "region", "team"]:
        out[c] = out[c].fillna("all")
    order = {"Critical": 0, "High": 1, "Medium": 2}
    out = out.sort_values(by="severity", key=lambda s: s.map(order), kind="stable").reset_index(drop=True)
    out.insert(0, "alert_id", [f"A-{i + 1:03d}" for i in range(len(out))])
    out = out[cols]
else:
    out = pd.DataFrame(columns=cols)
out.to_csv("alerts.csv", index=False)

print("Alerts raised:", len(out))
print(out[["alert_id", "name", "severity", "title"]].to_string(index=False))

# ---------- Check against the planted anomalies (for testing only) ----------
# The alert logic above never reads this column. We only look at it AFTER the alerts exist.
planted = inv.assign(anomaly=inv["anomaly"].fillna(""))
planted = planted[planted["anomaly"] != ""]
print("\nCheck against planted anomalies:")
for _, p in planted.iterrows():
    want = "usage_spike" if p["anomaly"] == "net_spike" else "capacity_trend"
    hit = ((out["resource_id"] == p["resource_id"]) & (out["alert_type"] == want)).any()
    print(f"  {p['anomaly']} on {p['resource_id']}: {'FOUND' if hit else 'MISSED'}")
vm_alerts = out[out["alert_type"].isin(["usage_spike", "capacity_trend"])]
false_alarms = vm_alerts[~vm_alerts["resource_id"].isin(planted["resource_id"])]
print(f"  VM alerts on machines with no planted anomaly: {len(false_alarms)}")