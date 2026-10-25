# SmartCloud Optimizer

Turns cloud utilization data into decisions: where resources are wasted, what to change, how much it saves, and how risky each change is. Built for TransfiNITTe 2026 (software track, Utho problem statement).

**All data is simulated.** Prices are illustrative. See `ASSUMPTIONS.md` for every rule and number.

## What it does

- **Utilization:** filter by region, environment and team; drill down into one machine's history (average, p95, p99).
- **Recommendations:** stop idle machines, schedule non-prod shutdowns, resize, delete orphaned volumes and IPs. Each has a reason, a monthly saving and a risk level. Accept or reject each one and the totals update live.
- **Peak-based sizing:** resizes use p99 (prod and bursty machines) or p95 (others), never averages, so a machine that peaks daily is never shrunk below its peak.
- **Alerts & Insights:** usage spikes, disks filling up (days until full), cost jumps with attribution, projected budget overrun. Each alert explains why it was raised.
- **What-if Simulator:** pick environments and a shutdown window and see the saving instantly, counting only machines that are quiet in that window.
- **Assumptions:** the rules behind every number.
- **Validation:** replays real readings on the proposed smaller plans, tests on days the engine never saw, compares with an average-based tool, and scores detection against hidden labels.

## Setup (Windows)

Needs Python 3.10 or newer.

```
cd D:\smartcloud-optimizer
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run

Run these in order from the project folder:

```
python generate_data.py
python engine.py
python cost_history.py
python alerts.py
python validation.py
streamlit run app.py
```

Then open http://localhost:8501. After changing any script or the data, re-run the scripts and press **C** in the browser to clear the Streamlit cache.

## Files

| File | Role |
|---|---|
| `generate_data.py` | writes `inventory.csv`, `metrics.parquet`, `orphans.csv` (100 VMs, 30 days, 15-minute readings) |
| `engine.py` | reads those, writes `recommendations.csv` |
| `cost_history.py` | writes `cost_history.csv`, `cost_drivers.csv`, `budget.csv` |
| `alerts.py` | writes `alerts.csv` |
| `validation.py` | writes the `validation_*.csv` files |
| `app.py` | the Streamlit dashboard |
| `ASSUMPTIONS.md` | pricing, thresholds, data generation, simplifications |

The scripts communicate through data files, not by importing each other.

## Known limitations

- Data is pre-generated, not real-time, and cleaner than real data.
- A stopped machine is counted as costing nothing (in reality its disk is still billed).
- Savings in different tabs overlap and should not be added together.
- Rightsizing looks at CPU and memory only.
- Three months of cost history is only enough for rough trends.