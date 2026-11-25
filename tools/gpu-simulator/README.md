# NVIDIA GPU simulator

Twelve fake GPU servers, 96 cards across five generations, for a MetricsHub agent to poll alongside the real servers. They exist so the NVIDIA dashboards can be built and demonstrated without owning a GPU, and so the failure cases can be shown on demand rather than waited for.

Each server answers `/metrics` like a `dcgm-exporter`. The field set and the label shapes come from the `NvidiaDCGMExporter` connector's own emulator, so whatever the connector parses from a real exporter it parses from these. What the simulator adds is a machine and time: the fixture's fields are arranged into cards and baseboards that exist as products, a per-GPU load envelope drives the gauges, and a background ticker advances the counters so `rate()` and `increase()` return something worth charting.

## Running it

```bash
python fake-dcgm-exporter.py --fleet --port 9401
```

That serves all twelve on 9401 to 9412 from one process. The `--port` matters, because `--fleet` counts up from 9400 by default and the resource snippet expects 9401.

Then paste the resources from `metricshub-resources.yaml` at the end of an existing resource group's `resources:` list in the agent's config, and save. The agent watches its own config directory and reloads differentially, so nothing else it monitors gets rescheduled and no restart is needed. The dashboards pick the fleet up from there.

In containers instead, one per server:

```bash
docker compose up -d --build
```

## The fleet

| Servers | Cards | Baseboard | What it shows |
|---|---|---|---|
| `dev-nvidia-02` to `-05` | 8 x H100 80GB HBM3 | 4 NVSwitch | The bulk of the fleet. `-04` has a card throttling on temperature, `-05` runs bursty. |
| `dev-nvidia-06`, `-07` | 8 x H200 | 4 NVSwitch | The newest silicon, steady training. Card 3 of `-06` falls off the bus 15 minutes after the exporter starts: it stops reporting entirely, and the two NVSwitch ports that lead to it go down. |
| `dev-nvidia-08` | 8 x B200 | 2 NVSwitch | A Blackwell pilot, bursty, with one NVSwitch port down. Card 5, the one at the far end of that port, reports a degraded fabric, which is what lists it as an unhealthy GPU. |
| `dev-nvidia-09` to `-12` | 8 x A100-SXM4-80GB | 6 NVSwitch | The older cluster, still in service. `-09` and `-11` have a partitioned card, `-10` has ECC errors, `-12` has a card that needs a reset. |
| `dev-nvidia-13` | 8 x L40S | none | PCIe cards, idle. No NVLink and no NVSwitch at all, which is the common enterprise case and the one that proves the fabric panels cope with having nothing to show. |

Five problems across 96 cards, about 5%, so the issue panels have something to say without the fleet reading as though it were on fire.

## Fitting into an existing site

The resources go inside an existing resource group, so the servers take its `site` and sit beside the others on every dashboard, under the same site metrics. Their data cannot be filtered out by site afterwards, and what reaches Prometheus stays there until it ages out.

The host names are examples. Change them in the script, the resource file and the compose file so they follow the naming of the other servers. The model is visible in `hw.gpu.model`, as it would be on real hardware.

Like a real exporter, the served output has no banner and responses take a few tens of milliseconds.

Each resource polls `localhost`. The comments in `metricshub-resources.yaml` show how to give each one its own hostname instead.

## Where the shape comes from

`payload-canonical.txt` is a parsing fixture, not a machine. One host in it carries a V100, an A100 and an H100 at once, four cards beside four NVSwitch chips, which is not a product anyone sells, and its enum fields carry one of every value so the connector's parsing gets exercised. Taken literally it produces a fleet where every host has a broken fabric.

So the field set comes from the fixture and the machine shape comes from the generation table in the script. That table sets the model name, the memory and BAR1 totals, the board power limit, the clock and temperature ceilings, the PCIe generation, the NVLink count per card and the NVSwitch count per baseboard. Memory, power, switch count and links are published figures. The clock and temperature ceilings are typical rather than measured, and the Blackwell ones are the least certain.

Current readings are retuned with the limits, so an H100 draws hundreds of watts against its own 700W ceiling rather than reporting a V100's numbers.

Health is the default and a fault is what takes it away. Port status, fabric manager status, fabric health, switch reset flags, row remapping, bank availability, the recovery action and the DCGM health watches are all forced to healthy values unless a fault says otherwise. Without that, the fixture's deliberately varied enums give every host about ten failures and the faults that tell a story are lost among them.

## What varies, and what does not

Identity and limits are fixed: models, PCI bus ids, NVSwitch ids, memory and BAR1 totals, clock and power limits, PCIe link generation and width, temperature limits. GPU UUIDs are fixed per host and differ between hosts.

Every card has its own character, taken from a hash of its host and index: its own steady level, its own phase, its own checkpoint timing and its own memory footprint. This matters more than it sounds. An earlier version seeded the phase as `host * 17 + index`, which put consecutive cards a ninety-seventh of a cycle apart, so all eight on a baseboard moved as one and a table of the fleet showed the same number on every row. Across a training fleet the levels now spread from about 48% to 97%, which is what a real job looks like once one card becomes the straggler.

Gauges follow that load. Utilization scales from the shipped reading, taken as the value at full load, so a card that ships reporting 0 on an engine keeps reporting 0 and absent engines stay absent. Temperature and SM clock track the load with their own floors, so an idle card still sits above ambient.

Power is a share of the card's own limit: about 12% idle, just under the cap at full load, and never above it, because real cards are power capped. The energy counter integrates that same power, so energy over a period agrees with the power shown for it.

Memory is a share of the card's own total, not of whatever the fixture shipped, and it moves slowly because a job holds its working set for its lifetime. A MIG slice holds a share of its own slice, and its profile name follows the card: `1g.10gb` on an 80 GB A100. Scaling the used figure and the total by the same generation factor, which is what the limits pass does to everything else, would leave every card in the fleet reporting an identical memory utilization.

Counters advance once a second from the instantaneous load, not per scrape, so two pollers see the same numbers and nothing double counts. Error counters are whole events. Energy is not. The violation timers stay at zero unless a fault moves them, for the same reason the status fields default to healthy.

Every counter starts at zero when the simulator starts. A counter that came back at the fixture's lifetime value would read to Prometheus as a reset followed by that whole lifetime arriving at once, which put a 200 GB/s spike on every switch port and a burst of errors on every card after each restart.

The switch port throughput fields count in units of 131072 bytes, as DCGM does, and the connector multiplies by that to get bytes. Each port follows the card at its other end, so ports differ the way cards do.

The GPU's own PCIe and NVLink throughput fields are different. They are rates rather than running totals, which is what the connector expects of them, so they rise and fall with the load. The fixture reads 0 on all of them, so the full-load figure comes from the card instead: each NVLink link at the same 12 GB/s as a switch port, and PCIe at 4 or 8 GB/s toward the card depending on its generation, with a quarter of that going back. Each NVLink direction drifts by a few percent on its own and receive sits 3% under transmit, so the two never read the same.

## Options

```
--host-name NAME      value of the Hostname label the exporter reports
--gen GENERATION      v100, a100, h100, h200, b200 or l40s
--gpus N              cards on the baseboard
--mig                 partition the first card into MIG instances
--profile PROFILE     training, bursty or idle
--seed N              shifts the load cycle so the fleet does not move in step
--fault GPU=KIND      thermal, ecc, xid, pcie or fabric, repeatable
--down-link SW:LINK   take one NVSwitch port down, e.g. nvswitch1:2
--port PORT           default 9400
--fleet               serve all twelve on consecutive ports in one process
```

Identities come from `--host-name`, not from `--seed`. Each host's GPU UUIDs are derived from its name, so no two hosts report the same card, while PCI bus ids and NVSwitch ids stay the same across hosts because those are slots rather than parts. `--seed` only shifts the load cycle, so give each host its own or the whole fleet idles and peaks together.

Faults work on any card, because every card is built from the fixture's richest personality and so carries the whole field set. Switch ids run from `nvswitch0`, and each switch has ports 0 to 2.

## Requirements

The agent needs the `NvidiaDCGMExporter` connector, which is not in the shipped library yet. It arrives with **MetricsHub 3.9.09 or newer**, which is also the first engine carrying the `toTable` compute that the NVSwitch monitor jobs use. On an older agent the connector fails to deserialize on that one unknown type id and no GPU data is produced at all, not even the GPU job.

A later version of the connector adds the GPU temperature limits, the seven DCGM health checks (pcie, memory, inforom, thermal, power, driver and critical-xid), `hw.gpu.recovery_action`, and the memory health metrics `hw.gpu.memory.condition_active`, `hw.gpu.memory.remapped_rows`, `hw.gpu.memory.row_remap.bank.count` and `hw.gpu.memory.state`. An earlier connector produces everything else. Its only GPU health statuses are fabric and fabric manager, so the fabric fault on `dev-nvidia-08` is the one that lists a card as unhealthy. The thermal, ECC and XID faults then show only as throttle time and error counts, the reset request on `dev-nvidia-12` does not show, and the memory health panels stay empty.

## The payload

`payload-canonical.txt` is the output of the connector's test emulator, checked in so the image needs nothing else at build time. It changes only when the connector's field set does.

## What this is not

No serial numbers or VBIOS versions. A stock `dcgm-exporter` adds them as labels on every series, but the connector does not read them, so putting them in this payload would only produce labels nothing consumes. Where those values are wanted, a second connector, `NvidiaSmi`, pointed at the same host over its own transport, provides them.

The driver version is the exception. Every series carries `DCGM_FI_DRIVER_VERSION="610.43.02"`, the label a stock exporter adds, for a connector version that maps it to `driver_version`. Every simulated host runs the same driver, so the whole fleet reads as a single version.

The numbers are plausible, not physical. Power does not follow a real power model, temperature does not follow a real thermal model, and the load envelope is a shape chosen to read well on a chart rather than a workload trace. Field availability is the fixture's, which was built on V100, A100 and H100 vocabularies, so the newer generations borrow their field coverage and only their limits are their own.

It is good enough to build dashboards against and to demonstrate what the connector reports. It is not good enough to validate a threshold or a capacity calculation.
