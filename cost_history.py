import numpy as np
import pandas as pd

# ---------- Settings (listed in ASSUMPTIONS.md later) ----------
SEED = 5                       # change this number to get a different random history
MONTHS = ["2026-08", "2026-09", "2026-10"]
BUDGET_MARGIN = 0.20           # approved monthly budget = first month's cost + 20%
# Random month-on-month growth range for each group (min, max)
GROWTH_RANGE = {
    "Prod": (0.02, 0.06),
    "Staging": (0.03, 0.08),
    "Dev": (0.05, 0.30),
    "Orphans": (0.10, 0.30),
}

rng = np.random.default_rng(SEED)
inv = pd.read_csv("inventory.csv")
orph = pd.read_csv("orphans.csv")

# Today's real cost per group = the LAST month
final = {
    "Prod": float(inv[inv["environment"] == "prod"]["monthly_cost"].sum()),
    "Staging": float(inv[inv["environment"] == "staging"]["monthly_cost"].sum()),
    "Dev": float(inv[inv["environment"] == "dev"]["monthly_cost"].sum()),
    "Orphans": float(orph["monthly_cost"].sum()),
}

# ---------- Work backwards with random growth ----------
values = {}
for cat, last in final.items():
    lo, hi = GROWTH_RANGE[cat]
    g1 = rng.uniform(lo, hi)     # growth from month 1 to month 2
    g2 = rng.uniform(lo, hi)     # growth from month 2 to month 3
    m3 = last
    m2 = m3 / (1 + g2)
    m1 = m2 / (1 + g1)
    values[cat] = [m1, m2, m3]

rows = []
for i, month in enumerate(MONTHS):
    for cat in final:
        rows.append({"month": month, "category": cat,
                     "cost": int(round(values[cat][i])), "note": ""})
hist = pd.DataFrame(rows)

# Label the biggest single-month rise so the alert can mention it
hist["note"] = hist["note"].astype(object)
pivot = hist.pivot(index="month", columns="category", values="cost")
steps = pivot.diff().dropna()
big_month = steps.sum(axis=1).idxmax()
big_cat = steps.loc[big_month].idxmax()
hist.loc[(hist["month"] == big_month) & (hist["category"] == big_cat), "note"] = (
    f"{big_cat} cost grew the most this month")
hist.to_csv("cost_history.csv", index=False)

# ---------- Drivers: how much each group added ----------
totals = pivot.sum(axis=1)
G = totals.iloc[-1] - totals.iloc[0]
text = {
    "Dev": "Dev spend rose as new environments were added.",
    "Orphans": "Unattached volumes and IPs keep billing because nobody deleted them.",
    "Prod": "Production spend rose with usage (random illustrative growth).",
    "Staging": "Staging spend rose alongside production (random illustrative growth).",
}
drv = []
for cat in final:
    inc = pivot[cat].iloc[-1] - pivot[cat].iloc[0]
    drv.append({
        "driver": cat,
        "increase_rs_per_month": int(inc),
        "explanation": f"{text[cat]} Went from Rs {pivot[cat].iloc[0]:,} to Rs {pivot[cat].iloc[-1]:,} per month.",
        "share_of_growth_pct": int(round(inc / G * 100)) if G > 0 else 0,
    })
drivers = pd.DataFrame(drv).sort_values("increase_rs_per_month", ascending=False)
drivers.to_csv("cost_drivers.csv", index=False)

# ---------- Budget ----------
budget = round(totals.iloc[0] * (1 + BUDGET_MARGIN), -3)
pd.DataFrame([{"monthly_budget": int(budget)}]).to_csv("budget.csv", index=False)

# ---------- Print a summary ----------
print("Monthly total cost (Rs):")
print(totals.to_string())
print(f"\nGrowth first to last month: {(totals.iloc[-1] / totals.iloc[0] - 1) * 100:.0f}%")
print(f"Approved monthly budget (assumption): Rs {budget:,.0f}")
print("\nWhy it grew:")
print(drivers[["driver", "increase_rs_per_month", "share_of_growth_pct"]].to_string(index=False))