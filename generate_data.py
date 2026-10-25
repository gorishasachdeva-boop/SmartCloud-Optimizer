import os
import numpy as np
import pandas as pd

np.random.seed(42)  # same fake data every run
rng = np.random.default_rng(7)  # separate generator for the NEW columns
rng2 = np.random.default_rng(21)  # separate generator for creation dates
# How old each environment's VMs are, in days before the data starts (min, max)
AGE_RANGE = {"prod": (200, 900), "staging": (100, 500), "dev": (20, 300)}

# Illustrative pricing (fake, listed in ASSUMPTIONS.md)
PLANS = {
    "S1":  {"vcpu": 1,  "ram": 2,  "cost": 700},
    "S2":  {"vcpu": 2,  "ram": 4,  "cost": 1400},
    "S4":  {"vcpu": 4,  "ram": 8,  "cost": 2800},
    "S8":  {"vcpu": 8,  "ram": 16, "cost": 5600},
    "S16": {"vcpu": 16, "ram": 32, "cost": 11200},
}

regions = ["Noida", "Bangalore", "Mumbai"]
teams = ["payments", "search", "platform", "data", "mobile"]


def pick_archetype(env):
    """Decide how this machine behaves, based on its environment."""
    if env == "prod":
        return np.random.choice(
            ["well_sized", "low_usage", "bursty", "batch"],
            p=[0.40, 0.25, 0.25, 0.10],
        )
    return np.random.choice(
        ["office_hours", "low_usage", "idle"],
        p=[0.75, 0.15, 0.10],
    )


# ---------- 1. Inventory table ----------
rows = []
for i in range(1, 101):
    env = np.random.choice(["prod", "staging", "dev"], p=[0.35, 0.25, 0.40])
    team = np.random.choice(teams)
    plan = np.random.choice(list(PLANS.keys()))
    rows.append({
        "resource_id": f"vm-{i:03d}",
        "name": f"{env}-{team}-{i:03d}",
        "type": "VM",
        "region": np.random.choice(regions),
        "environment": env,
        "team": team,
        "plan": plan,
        "vcpu": PLANS[plan]["vcpu"],
        "ram_gb": PLANS[plan]["ram"],
        "monthly_cost": PLANS[plan]["cost"],
        "disk_gb": PLANS[plan]["vcpu"] * 40,
        "creation_date": (pd.Timestamp("2026-09-24")
                          - pd.Timedelta(days=int(rng2.integers(*AGE_RANGE[env])))
                          ).strftime("%Y-%m-%d"),
        "archetype": pick_archetype(env),  # ground truth label
    })

inventory = pd.DataFrame(rows)

# --- Plant two genuine anomalies in prod VMs (ground truth, for validation later) ---
inventory["anomaly"] = ""
prod_ok = inventory[(inventory["environment"] == "prod")
                    & (inventory["archetype"] == "well_sized")]["resource_id"].tolist()
if len(prod_ok) < 2:  # fallback: any prod VM
    prod_ok = inventory[inventory["environment"] == "prod"]["resource_id"].tolist()
NET_SPIKE_VM = prod_ok[0]   # network spikes for 3 days
DISK_FILL_VM = prod_ok[1]   # disk fills ~2% per day
inventory.loc[inventory["resource_id"] == NET_SPIKE_VM, "anomaly"] = "net_spike"
inventory.loc[inventory["resource_id"] == DISK_FILL_VM, "anomaly"] = "disk_fill"

inventory.to_csv("inventory.csv", index=False)


# ---------- 2. Usage readings (30 days, every 15 minutes) ----------
timestamps = pd.date_range("2026-09-24", periods=30 * 96, freq="15min")
hour = timestamps.hour.values + timestamps.minute.values / 60
is_weekday = timestamps.day_of_week.values < 5
n = len(timestamps)
day = np.arange(n) / 96  # 0.0 ... 29.99 (days since start)


def make_cpu(archetype):
    noise = np.random.normal(0, 1, n)
    if archetype == "well_sized":
        cpu = 50 + 15 * np.sin((hour - 9) / 24 * 2 * np.pi) + noise * 4
    elif archetype == "low_usage":
        cpu = 7 + noise * 1.5
    elif archetype == "bursty":
        cpu = 6 + noise * 1.5
        spike = (hour >= 14) & (hour < 14.5)       # 2:00 to 2:30 PM daily
        cpu[spike] = 88 + noise[spike] * 2
    elif archetype == "batch":
        cpu = 3 + noise * 1
        job = (hour >= 1) & (hour < 3)             # 1 AM to 3 AM nightly
        cpu[job] = 90 + noise[job] * 2
    elif archetype == "office_hours":
        working = is_weekday & (hour >= 9) & (hour < 19)
        cpu = np.where(working, 40 + noise * 6, 2 + np.abs(noise) * 0.5)
    else:  # idle
        cpu = 1 + np.abs(noise) * 0.3
    return np.clip(cpu, 0, 100)


frames = []
for _, vm in inventory.iterrows():
    cpu = make_cpu(vm["archetype"])
    mem = np.clip(20 + cpu * 0.4 + np.random.normal(0, 2, n), 0, 100)

    # --- disk (% full): slow normal growth, or the planted fast fill ---
    if vm["anomaly"] == "disk_fill":
        disk = 25 + day * 2.0 + rng.normal(0, 0.3, n)        # +2% every day
    else:
        disk = rng.uniform(30, 60) + day * 0.03 + rng.normal(0, 0.2, n)

    # --- network (Mbps) follows CPU a little; planted 3-day spike ---
    net = 5 + cpu * 0.6 + np.abs(rng.normal(0, 1.5, n))
    if vm["anomaly"] == "net_spike":
        spike_days = (day >= 24) & (day < 27)                # days 24, 25, 26
        net[spike_days] = net[spike_days] * 10

    # --- disk IOPS follows CPU ---
    iops = 50 + cpu * 8 + np.abs(rng.normal(0, 15, n))

    frames.append(pd.DataFrame({
        "resource_id": vm["resource_id"],
        "timestamp": timestamps,
        "cpu_pct": cpu.round(1),
        "mem_pct": mem.round(1),
        "disk_pct": np.clip(disk, 0, 100).round(1),
        "net_mbps": net.round(1),
        "net_in_mbps": (net * 0.35).round(1),
        "net_out_mbps": (net * 0.65).round(1),
        "iops": iops.round(0),
    }))

metrics = pd.concat(frames, ignore_index=True)
metrics.to_parquet("metrics.parquet", index=False)


# ---------- 3. Orphans (only written if orphans.csv does not exist yet) ----------
# 8 unattached volumes (Rs 6 per GB per month) and 5 unused public IPs (Rs 250 per month)
if not os.path.exists("orphans.csv"):
    rng_o = np.random.default_rng(11)   # its own generator, so nothing else changes
    orphan_rows = []
    for k in range(1, 9):
        size = int(rng_o.choice([50, 100, 200, 500]))
        orphan_rows.append({
            "resource_id": f"vol-{k:03d}",
            "type": "Volume",
            "region": str(rng_o.choice(regions)),
            "size_gb": size,
            "monthly_cost": size * 6,
            "days_unattached": int(rng_o.integers(35, 201)),
        })
    for k in range(1, 6):
        orphan_rows.append({
            "resource_id": f"ip-{k:03d}",
            "type": "Public IP",
            "region": str(rng_o.choice(regions)),
            "size_gb": 0,
            "monthly_cost": 250,
            "days_unattached": int(rng_o.integers(35, 201)),
        })
    pd.DataFrame(orphan_rows).to_csv("orphans.csv", index=False)
    print("orphans.csv created (it did not exist before)")
else:
    print("orphans.csv already exists, left unchanged")


# ---------- 4. Quick check ----------
print("Machines:", len(inventory), "| Readings:", len(metrics))
print("Total monthly cost: Rs", inventory["monthly_cost"].sum())
print("Planted anomalies:  net_spike ->", NET_SPIKE_VM, "| disk_fill ->", DISK_FILL_VM)

check = metrics.merge(inventory[["resource_id", "archetype"]], on="resource_id")
summary = check.groupby("archetype")["cpu_pct"].agg(
    average="mean", p95=lambda s: s.quantile(0.95), peak="max"
).round(1)
print(summary)