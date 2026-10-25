# SmartCloud Optimizer: Assumptions

All data is **simulated**. Every figure in the app can be traced to a rule on this page.

## 1. Pricing (illustrative, not real provider prices)

| Plan | vCPU | RAM (GB) | Cost (Rs/month) |
|---|---|---|---|
| S1 | 1 | 2 | 700 |
| S2 | 2 | 4 | 1,400 |
| S4 | 4 | 8 | 2,800 |
| S8 | 8 | 16 | 5,600 |
| S16 | 16 | 32 | 11,200 |

- Unattached storage volume: Rs 6 per GB per month.
- Unused public IP: Rs 250 per month.
- Costs are flat monthly prices. There is no hourly billing, discount, or tax.
- Disk size is assumed to be 40 GB per vCPU on the current plan (S1 = 40 GB, S2 = 80 GB, S4 = 160 GB, S8 = 320 GB, S16 = 640 GB). A resize is assumed to change vCPU and RAM only, and the disk stays at its current size, so a resize does not affect disk usage or the disk-full alerts. Plan prices cover compute only in this comparison. Only detached volumes and unused public IPs are billed as separate items. This is a simplification.

## 2. How the data was generated

- 100 VMs, random seed 42 (same data every run). Environments: prod 35%, staging 25%, dev 40%. Regions: Noida, Bangalore, Mumbai. Teams: payments, search, platform, data, mobile.
- 30 days of readings every 15 minutes (2,880 per VM), starting 24 Sep 2026.
- Each VM has a hidden behaviour label (archetype). The engine never reads it. It is only used to check the engine's answers.

| Archetype | Where | CPU behaviour |
|---|---|---|
| well_sized | prod | about 50%, smooth daily wave |
| low_usage | prod, dev, staging | about 7% all the time |
| bursty | prod | about 6%, but about 88% for 30 minutes at 2 PM every day |
| batch | prod | about 3%, but about 90% from 1 AM to 3 AM every night |
| office_hours | dev, staging | about 40% on weekdays 9 AM to 7 PM, about 2% otherwise |
| idle | dev, staging | about 1% |

- Memory % = 20 + 0.4 x CPU % + noise.
- Disk % starts between 30 and 60 and grows 0.03 points per day. Network (Mbps) = 5 + 0.6 x CPU % + noise. IOPS = 50 + 8 x CPU % + noise.
- Network traffic is stored as a total plus an in/out split: 35% in, 65% out (a fixed assumption, not measured).
- Two planted anomalies in prod VMs: one has network traffic 10x higher for 3 days (days 24 to 26), one has a disk filling 2 points per day.
- Creation dates are random: prod VMs are 200 to 900 days old, staging 100 to 500, dev 20 to 300 (counted back from 24 Sep 2026).
- Orphans: 8 unattached volumes (50, 100, 200 or 500 GB) and 5 unused IPs, each unattached for 35 to 200 days.
- The inventory lists VMs. Unattached volumes and unused public IPs are listed separately in orphans.csv and shown in the Utilization tab. VM boot disks are not listed as separate resources.
- **Cost history is simulated and shorter than the brief's scenario.** The brief describes a bill that grew 40% in six months. We simulate only 3 months (Aug to Oct 2026): the latest month equals today's real bill per group (prod, staging, dev, orphans), and the earlier months are worked out backwards with random monthly growth rates (seed 5): prod 2 to 6%, staging 3 to 8%, dev 5 to 30%, orphans 10 to 30%. The total growth is therefore whatever those random rates produce, not 40%.
- The monthly budget is an assumption: first month's cost plus 20%, rounded to the nearest Rs 1,000.

## 3. Recommendation rules

Each VM gets at most one action, checked in this order. All statistics are measured over the 30 days.

| Order | Action | Rule | Saving |
|---|---|---|---|
| 1 | Stop/delete | p99 CPU below 5% | 100% of VM cost |
| 2 | Schedule shutdown | non-prod, off-hours p95 CPU below 5%, working-hours average CPU above 15% | 64% of VM cost |
| 3 | Resize | smallest plan where the projected peak CPU is 70% or less and memory is 80% or less, and cheaper than now | current cost minus new plan cost |
| 4 | Keep | none of the above | 0 |
| extra | Delete orphan | unattached for 30 days or more | 100% of its cost |

- **Peak, not average.** Resize uses p99 for prod VMs and for bursty VMs (p99 above 5 times the average), and p95 for the rest. Averages are never used for sizing, so a VM that peaks daily is never shrunk below its peak.
- **Projected peak** = peak usage in vCPUs (or GB) divided by the vCPUs (or GB) of the new plan.
- Off-hours means weekdays outside 8 AM to 8 PM, plus all of Saturday and Sunday. That is 108 of 168 hours, so a scheduled shutdown saves 64%.
- Savings are measured against each item's current monthly price. They are not discounted for the time it takes to make the change.

**Risk levels**

| Action | Risk |
|---|---|
| Resize | High if projected CPU is above 65%. Medium if above 50% or the VM is bursty. Otherwise Low. |
| Stop/delete | Medium for prod, Low for non-prod |
| Schedule shutdown | Low |
| Delete orphan | Low (volumes: take a snapshot first) |

**Worked example (illustrative numbers).** A prod VM on S4 (4 vCPU, 8 GB, Rs 2,800) has p99 CPU 30% and p99 memory 32%.
- Peak use: 0.30 x 4 = 1.2 vCPU and 0.32 x 8 = 2.56 GB.
- On S2: CPU 1.2 / 2 = 60% (limit 70%), memory 2.56 / 4 = 64% (limit 80%). It fits.
- On S1 it would not fit (CPU 120%), so S2 is the smallest safe plan.
- Saving = 2,800 - 1,400 = Rs 1,400 per month, or Rs 16,800 per year. Risk is Medium (projected CPU above 50%).

## 4. Alert rules

| Alert | Rule |
|---|---|
| Usage spike | Normal behaviour is learned per VM, per time of day, separately for weekdays and weekends, from the first 21 days. An alert needs the 3-hour rolling median to stay more than 5 typical variations above normal for at least 4 hours, during the last 9 days. Daily repeating peaks are therefore not flagged. Critical if 8x normal or more, High if 4x or more, otherwise Medium. Checked for network traffic and CPU. |
| Capacity trend | Straight-line fit on the last 14 daily averages of disk or memory. Needs growth above 0.1 points per day and a fit quality (R2) of 0.6 or more. Alerts if full within 30 days (Critical within 10). |
| Cost jump | A month-on-month rise of more than 1.5 times the smallest monthly rise. The alert names which groups caused it. With only 3 months of history there are only 2 monthly changes, so the smallest rise is used as the "typical" one. |
| Budget overrun | Straight-line trend over the monthly totals, projected 3 months ahead. Alerts if current or projected spend is above the budget. |

- Every alert carries a plain-English explanation of why it was raised, with the numbers it used.
- Account-level alerts (cost jump, budget overrun) belong to no single VM, so they are always shown, whatever the sidebar filters say.

## 5. What-if simulator

- Saving = monthly cost x share of the week the machine is off.
- The shutdown window can cross midnight (for example 20:00 to 08:00). Weekends can be switched on or off. With the defaults the machine is off 108 of 168 hours (64%).
- A machine counts as safe if its p95 CPU during the shutdown window is at or below the safety limit (default 5%). Unsafe machines are left out unless the user ticks the risky option.
- Machines already recommended for Stop/delete are excluded.
- The what-if saving and the Recommendations saving overlap (a machine can appear in both), so they must not be added together.

## 6. Validation

The Validation tab tests the engine. All of it uses simulated data, so the results show that the logic is consistent and safe on this data. They do not prove it would be right on messy real data.

- **Resize replay.** Each recommended resize is replayed on its new, smaller plan using all 30 days of readings. A resize passes if no single reading reaches 100% CPU or memory. A second line checks that p99 stays within the planning limits (70% CPU, 80% memory). That second line is not independent evidence, because prod and bursty VMs were sized on p99 with the same limits.
- **Average-based comparison.** The same VMs are sized on average CPU and memory instead of peaks, using the same 70% and 80% limits, and each result is replayed the same way. This shows how many resizes would run out of capacity at the daily peak. Many of the machines it breaks are office-hours VMs, which this engine handles with a shutdown schedule instead of a resize.
- **Detection accuracy.** Precision and recall of the Stop/delete, Schedule shutdown and Delete orphan recommendations against the hidden behaviour labels. The data is clean, so these scores are higher than real data would give. The orphan check mostly tests the pipeline, because orphans are listed in a file rather than discovered.
- **Alert check.** Did the alerts find the two planted anomalies, and how many alerts were raised on VMs with no planted problem.
- **Holdout test.** The sizing rule is re-run using only the first 21 days, and the plan it picks is replayed on the last 9 days, which it never saw. It also reports how many machines get the same plan from 21 days as from 30. The split is of one simulated month, and real workloads can change more between periods than this data does.

## 7. Using your own data (Upload tab)

- Required files: inventory.csv (`resource_id, name, region, environment, team, plan, vcpu, ram_gb, monthly_cost`) and metrics.csv (`resource_id, timestamp, cpu_pct, mem_pct`). Optional columns: `disk_pct, net_mbps, iops`. Optional files: orphans.csv and plans.csv (`plan, vcpu, ram_gb, monthly_cost`).
- The app checks the files and shows a clear message for missing columns, empty or text values, duplicate IDs, an unknown environment, unreadable timestamps and fewer than 7 days of data. Percentages outside 0 to 100 are clipped, and duplicate readings are dropped, with a warning.
- Uploaded data is saved in a separate `user_data` folder, and the same scripts run on it. The sample data is never changed.
- Without plans.csv, resize targets and savings use the illustrative price list above, not the user's real prices.
- The monthly cost history is still simulated for uploaded data: the latest month is the uploaded bill and earlier months are random.
- If the data covers less than 30 days, the alert baseline and the holdout split shrink to 70% of the available days. The reason text in recommendations still says "30 days".
- The alert thresholds are tuned for 15-minute readings. Other intervals work, but spike alerts may be less sensitive.
- Without the hidden behaviour labels, precision and recall cannot be measured, so the validation shows the resize replay, the holdout test and the comparison only.
- Disk and network alerts are skipped if those columns are missing.

## 8. Simplifications and limits

- Data is pre-generated, not real-time.
- A stopped VM is assumed to cost nothing. In reality its disk is still billed, so real savings are a little lower.
- Savings from different tabs overlap (for example a VM can be resizable and schedulable). Do not add them together. Inside the Recommendations tab each VM has only one action, so totals there do not double count.
- Rightsizing looks at CPU and memory only. IOPS and network are recorded and charted, but they do not affect the decision.
- A resize changes vCPU and RAM only. The disk is assumed to stay the same size.
- The simulated memory is tied to CPU, which real systems are not.
- Prices are illustrative. Real providers have discounts, reserved instances and egress fees that are not modelled.
- Three months of cost history is very little, and it is simulated. Trend and budget projections are rough straight lines, not forecasts with confidence intervals.
- The scripts assume 30 days of 15-minute readings. Shorter or coarser data works with warnings.
- The plan table is written in two files (`generate_data.py` and `engine.py`) and must be kept in sync, unless the user uploads a plans.csv.
- The app only recommends. A person accepts or rejects every change, and nothing is applied to any cloud account.