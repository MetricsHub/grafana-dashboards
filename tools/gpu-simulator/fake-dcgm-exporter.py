#!/usr/bin/env python3
"""A fake dcgm-exporter whose readings move, so a MetricsHub agent can poll it like a real
GPU server.

The field set and the label shapes come from the NvidiaDCGMExporter connector's own test
emulator, so whatever the connector parses from a real exporter it parses from this. What this adds is time: a per-GPU load envelope drives the gauges and a
background ticker advances the counters, so rate() and increase() produce something to look at.

One process serves one simulated server, which is how it runs in a container. --fleet serves
several on consecutive ports in a single process, for running it straight off a workstation.

  fake-dcgm-exporter.py --host-name gpu-node-01 --profile training
  fake-dcgm-exporter.py --host-name gpu-node-02 --profile bursty --fault 0=thermal
  fake-dcgm-exporter.py --fleet
"""
import argparse
import hashlib
import http.server
import math
import random
import re
import socketserver
import sys
import threading
import time

REF = 0.60

# ---------------------------------------------------------------- field classes
# FB_USED_RATIO is deliberately absent: it is driven from the card's memory share instead, so
# that it agrees with FB_USED rather than tracking the compute load.
RATIO = {
    "DCGM_FI_DEV_GPU_UTIL_RATIO",
    "DCGM_FI_PROF_SM_UTIL_RATIO", "DCGM_FI_PROF_TENSOR_UTIL_RATIO",
    "DCGM_FI_PROF_DRAM_UTIL_RATIO", "DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO",
    "DCGM_FI_PROF_FP16_UTIL_RATIO", "DCGM_FI_PROF_FP32_UTIL_RATIO",
    "DCGM_FI_PROF_FP64_UTIL_RATIO", "DCGM_FI_PROF_INT_UTIL_RATIO",
    "DCGM_FI_PROF_SM_OCCUPANCY_RATIO",
}
PERCENT = {"DCGM_FI_DEV_ENC_UTIL", "DCGM_FI_DEV_DEC_UTIL", "DCGM_FI_DEV_MEM_COPY_UTIL"}
POWER = {"DCGM_FI_DEV_BOARD_POWER_WATTS"}
SWITCH_POWER = {"DCGM_FI_DEV_NVSWITCH_POWER_VDD_WATTS", "DCGM_FI_DEV_NVSWITCH_POWER_DVDD_WATTS",
                "DCGM_FI_DEV_NVSWITCH_POWER_HVDD_WATTS"}
TEMP = {"DCGM_FI_DEV_GPU_TEMP_CELSIUS": 32.0, "DCGM_FI_DEV_MEMORY_TEMP_CELSIUS": 34.0,
        "DCGM_FI_DEV_NVSWITCH_TEMP_CELSIUS": 30.0}
CLOCK = {"DCGM_FI_DEV_SM_CLOCK"}
MEM_USED = {"DCGM_FI_DEV_FB_USED": "fb", "DCGM_FI_DEV_BAR1_USED": "bar1"}
MEM_FREE = {"DCGM_FI_DEV_FB_FREE": ("DCGM_FI_DEV_FB_TOTAL", "DCGM_FI_DEV_FB_USED",
                                    "DCGM_FI_DEV_FB_RESERVED"),
            "DCGM_FI_DEV_BAR1_FREE": ("DCGM_FI_DEV_BAR1_TOTAL", "DCGM_FI_DEV_BAR1_USED", None)}
# Throughput fields are rates, not running totals, so they rise and fall with the load.
RATE = {
    "DCGM_FI_DEV_NVLINK_TX_THROUGHPUT_TOTAL", "DCGM_FI_DEV_NVLINK_RX_THROUGHPUT_TOTAL",
    "DCGM_FI_DEV_NVLINK_THROUGHPUT_TOTAL",
    "DCGM_FI_PROF_PCIE_TX_BYTES", "DCGM_FI_PROF_PCIE_RX_BYTES",
    "DCGM_FI_PROF_NVLINK_TX_BYTES", "DCGM_FI_PROF_NVLINK_RX_BYTES",
    "DCGM_FI_PROF_NVLINK_TX_BYTES_PER_LINK", "DCGM_FI_PROF_NVLINK_RX_BYTES_PER_LINK",
    "DCGM_FI_PROF_NVLINK_L0_TX_BYTES", "DCGM_FI_PROF_NVLINK_L0_RX_BYTES",
    "DCGM_FI_PROF_NVLINK_L1_TX_BYTES", "DCGM_FI_PROF_NVLINK_L1_RX_BYTES",
}
COUNTER = {
    "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": ("energy", True),
    "DCGM_FI_DEV_POWER_VIOLATION": ("us_cap", True),
    "DCGM_FI_DEV_THERMAL_VIOLATION": ("us_thermal", True),
    "DCGM_FI_DEV_SYNC_BOOST_VIOLATION": ("zero", False),
    "DCGM_FI_DEV_BOARD_LIMIT_VIOLATION": ("zero", False),
    "DCGM_FI_DEV_RELIABILITY_VIOLATION": ("zero", False),
    "DCGM_FI_DEV_LOW_UTIL_VIOLATION": ("us_idle", False),
    "DCGM_FI_DEV_TOTAL_APP_CLOCKS_VIOLATION": ("us_idle", False),
    "DCGM_FI_DEV_TOTAL_BASE_CLOCKS_VIOLATION": ("us_idle", False),
    "DCGM_FI_DEV_CLOCKS_EVENT_REASON_HW_THERM_SLOWDOWN_NS": ("ns_thermal", True),
    "DCGM_FI_DEV_CLOCKS_EVENT_REASON_HW_POWER_BRAKE_SLOWDOWN_NS": ("zero", False),
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": ("ecc_c", True),
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": ("ecc_u", True),
    "DCGM_FI_DEV_PCIE_REPLAY_TOTAL": ("pcie", True),
    "DCGM_FI_DEV_PCIE_CORRECTABLE_ERROR_TOTAL": ("pcie", True),
    "DCGM_FI_DEV_NVLINK_CRC_ERROR_TOTAL": ("link", True),
    "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL": ("link", True),
    "DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_TOTAL": ("link", True),
    "DCGM_FI_DEV_NVLINK_RECOVERY_TOTAL": ("link", True),
    "DCGM_FI_DEV_NVLINK_REPLAY_ERROR_TOTAL": ("link", True),
    "DCGM_FI_DEV_NVLINK_REPLAY_TOTAL": ("link", True),
    # The switch port throughput fields are cumulative counters, not instantaneous rates. The
    # connector says so outright, and treating them as rates left rate() with a series that
    # jumps around instead of climbing, so every port reported 0 B/s of traffic.
    "DCGM_FI_DEV_NVSWITCH_LINK_THROUGHPUT_TX": ("swbytes", True),
    "DCGM_FI_DEV_NVSWITCH_LINK_THROUGHPUT_RX": ("swbytes", True),
    "DCGM_FI_DEV_NVSWITCH_LINK_CRC_ERROR_TOTAL": ("swlink", False),
    "DCGM_FI_DEV_NVSWITCH_LINK_ECC_ERROR_TOTAL": ("swlink", False),
    "DCGM_FI_DEV_NVSWITCH_LINK_FLIT_ERROR_TOTAL": ("swlink", False),
    "DCGM_FI_DEV_NVSWITCH_LINK_RECOVERY_ERROR_TOTAL": ("swlink", False),
    "DCGM_FI_DEV_NVSWITCH_LINK_REPLAY_ERROR_TOTAL": ("swlink", False),
    "DCGM_EXP_XID_ERRORS_TOTAL": ("xid", False),
    "DCGM_EXP_XID_ERRORS_COUNT": ("xid", False),
}
COUNT_KINDS = {"ecc_c", "ecc_u", "pcie", "link", "swlink", "xid"}

COUNTER_RATES = {                       # per second at full load
    "energy": 1.0,                      # replaced in tick() by the card's own power
    # Every violation timer is zero by default, for the same reason the status fields are
    # healthy by default. A fleet where every card is throttling says nothing and buries the
    # one card that genuinely is. A fault supplies the rate instead.
    "us_cap": 0.0, "us_thermal": 0.0, "us_idle": 0.0,
    "ns_thermal": 0.0, "zero": 0.0,
    # One switch port in one direction at full load. DCGM counts fields 780 and 781 in units of
    # 131072 bytes, not bytes, and the connector's parser multiplies by that to get bytes. So
    # this is 12 GB/s expressed in the exporter's own unit: 1.2e10 / 131072. Accumulating in
    # bytes here instead put 146 petabytes on a single port. A Hopper NVLink runs 25 GB/s each
    # way, so a busy fabric at about half of that is right.
    "swbytes": 1.2e10 / 131072,
    "ecc_c": 0.0, "ecc_u": 0.0, "pcie": 0.0, "link": 0.0, "swlink": 0.0, "xid": 0.0,
}
# What the GPU-wide traffic fields read at full load, in bytes per second each way. Every card in
# the fixture reads 0 on them, and scaling 0 by the load left the NVLink and PCIe charts flat.
# NVLink carries each of the card's links at the switch port rate above, so the GPU view and the
# switch view of the fabric agree. PCIe carries the input pipeline, mostly toward the card.
NVLINK_LINK_RATE = 1.2e10
PCIE_RATE = {4: 4.0e9, 5: 8.0e9}        # toward the card, by PCIe generation
TRAFFIC = {
    "DCGM_FI_PROF_NVLINK_TX_BYTES": ("nvlink", "tx", 1.0),
    "DCGM_FI_PROF_NVLINK_RX_BYTES": ("nvlink", "rx", 1.0),
    # The DEV family counts in units of 1,024,000 bytes, which the connector multiplies by.
    "DCGM_FI_DEV_NVLINK_TX_THROUGHPUT_TOTAL": ("nvlink", "tx", 1 / 1024000),
    "DCGM_FI_DEV_NVLINK_RX_THROUGHPUT_TOTAL": ("nvlink", "rx", 1 / 1024000),
    "DCGM_FI_DEV_NVLINK_THROUGHPUT_TOTAL": ("nvlink", "both", 1 / 1024000),
    "DCGM_FI_PROF_PCIE_RX_BYTES": ("pcie", "rx", 1.0),
    "DCGM_FI_PROF_PCIE_TX_BYTES": ("pcie", "tx", 0.25),
}
FAULT_RATES = {
    "thermal": {"us_thermal": 400000.0, "ns_thermal": 6.0e8},
    "ecc": {"ecc_c": 0.012, "ecc_u": 0.0004},
    "xid": {"xid": 0.004},
    "pcie": {"pcie": 0.03},
    "downlink": {"swlink": 0.05},
    "fabric": {},                       # a status, not a rate, handled in render()
    "lost": {},                         # the card falls off the bus, handled in render() and tick()
}
# A lost card reports normally for this long after the exporter starts, so the agent discovers it
# before it disappears, as a real card that falls off the bus does.
LOST_AFTER = 900

# ------------------------------------------------------------- machine classes
# The payload is a parsing fixture, not a machine: one host in it carries a V100, an A100 and an
# H100 at once, four GPUs beside four NVSwitches, which is not a product you can buy. So the
# field SET comes from the payload and the machine SHAPE comes from here.
#
# Memory, board power, switch count and links per GPU are published figures. The clock and
# temperature ceilings are typical rather than measured, and the Blackwell ones are the least
# certain of them, which the README says out loud.
GEN = {
    "v100": dict(model="Tesla V100-SXM2-32GB", fb=32768, bar1=65536, power=300,
                 sm=1530, memclk=877, vidclk=1530, optemp=87, slowtemp=93,
                 pcie=4, links=6, switches=0),
    "a100": dict(model="NVIDIA A100-SXM4-80GB", fb=81920, bar1=65536, power=400,
                 sm=1410, memclk=1593, vidclk=1290, optemp=85, slowtemp=92,
                 pcie=4, links=12, switches=6),
    "h100": dict(model="NVIDIA H100 80GB HBM3", fb=81559, bar1=131072, power=700,
                 sm=1980, memclk=2619, vidclk=1755, optemp=87, slowtemp=92,
                 pcie=5, links=18, switches=4),
    "h200": dict(model="NVIDIA H200", fb=143771, bar1=131072, power=700,
                 sm=1980, memclk=3201, vidclk=1755, optemp=87, slowtemp=92,
                 pcie=5, links=18, switches=4),
    "b200": dict(model="NVIDIA B200", fb=183359, bar1=131072, power=1000,
                 sm=1965, memclk=4000, vidclk=1800, optemp=90, slowtemp=95,
                 pcie=5, links=18, switches=2),
    # A PCIe card. No NVLink and no NVSwitch, which is the common enterprise case and the one
    # that proves the fabric panels cope with having nothing to show.
    "l40s": dict(model="NVIDIA L40S", fb=46068, bar1=65536, power=350,
                 sm=2520, memclk=9001, vidclk=2250, optemp=90, slowtemp=95,
                 pcie=4, links=0, switches=0),
}

# The payload's own GPU 0 is a V100, so every scaled value is relative to these.
REF_LIMITS = dict(fb=32768, bar1=65536, power=300, sm=1530, memclk=877, vidclk=1530)

# Replaced outright by the generation's figure.
LIMIT_FIELDS = {
    "DCGM_FI_DEV_FB_TOTAL": "fb",
    "DCGM_FI_DEV_BAR1_TOTAL": "bar1",
    "DCGM_FI_DEV_BOARD_POWER_LIMIT_ENFORCED_WATTS": "power",
    "DCGM_FI_DEV_BOARD_POWER_LIMIT_REQUESTED_WATTS": "power",
    "DCGM_FI_DEV_MAX_SM_CLOCK": "sm",
    "DCGM_FI_DEV_MAX_MEM_CLOCK": "memclk",
    "DCGM_FI_DEV_MAX_VIDEO_CLOCK": "vidclk",
    "DCGM_FI_DEV_GPU_MAX_OP_TEMP_CELSIUS": "optemp",
    "DCGM_FI_DEV_GPU_TEMP_SLOWDOWN_CELSIUS": "slowtemp",
    "DCGM_FI_DEV_PCIE_MAX_LINK_GEN": "pcie",
    "DCGM_FI_DEV_PCIE_LINK_GEN": "pcie",
}
# Multiplied by the generation's figure over the reference, so a card reports a current value
# that sits sensibly against its own ceiling instead of a V100's.
SCALE_FIELDS = {
    "DCGM_FI_DEV_FB_USED": "fb", "DCGM_FI_DEV_FB_FREE": "fb",
    "DCGM_FI_DEV_FB_RESERVED": "fb",
    "DCGM_FI_DEV_BAR1_USED": "bar1", "DCGM_FI_DEV_BAR1_FREE": "bar1",
    "DCGM_FI_DEV_BOARD_POWER_WATTS": "power",
    "DCGM_FI_DEV_SM_CLOCK": "sm", "DCGM_FI_DEV_MEM_CLOCK": "memclk",
    "DCGM_FI_DEV_VIDEO_CLOCK": "vidclk",
}
# The fixture carries one of every enum value so the connector's parsing gets exercised, which
# on a fleet reads as every host having a broken fabric. Health is the default here and a fault
# is what takes it away. Values are the connector's own healthy codes: port 2 is ACTIVE, fabric
# manager 3 is running, health summary 1 is healthy, a health watch of 0 is PASS.
HEALTHY = {
    "DCGM_FI_DEV_NVSWITCH_LINK_STATUS": 2.0,
    "DCGM_FI_DEV_FABRIC_MANAGER_STATUS": 3.0,
    "DCGM_FI_DEV_FABRIC_HEALTH_SUMMARY": 1.0,
    "DCGM_FI_DEV_FABRIC_HEALTH_MASK": 0.0,
    "DCGM_FI_DEV_NVSWITCH_RESET_REQUIRED": 0.0,
    "DCGM_FI_DEV_SXID_FATAL_ERROR": 0.0,
    "DCGM_FI_DEV_SXID_NON_FATAL_ERROR": 0.0,
    "DCGM_FI_DEV_NVSWITCH_LINK_FATAL_ERRORS": 0.0,
    "DCGM_FI_DEV_NVSWITCH_LINK_NON_FATAL_ERRORS": 0.0,
    "DCGM_FI_DEV_ROW_REMAP_FAILED": 0.0,
    "DCGM_FI_DEV_SRAM_EXCEEDED": 0.0,
    "DCGM_EXP_GPU_HEALTH_STATUS": 0.0,
    # The fixture's card 0, which every card is built from, has remapped rows, a pending remap,
    # banks running low on spare rows and a request for a reset. A healthy card has none of that.
    "DCGM_FI_DEV_ROW_REMAP_CORRECTABLE_TOTAL": 0.0,
    "DCGM_FI_DEV_ROW_REMAP_UNCORRECTABLE_TOTAL": 0.0,
    "DCGM_FI_DEV_ROW_REMAP_PENDING": 0.0,
    "DCGM_FI_DEV_BANK_REMAP_AVAIL_MAX": 192.0,
    "DCGM_FI_DEV_BANK_REMAP_AVAIL_HIGH": 0.0,
    "DCGM_FI_DEV_BANK_REMAP_AVAIL_PARTIAL": 0.0,
    "DCGM_FI_DEV_BANK_REMAP_AVAIL_LOW": 0.0,
    "DCGM_FI_DEV_BANK_REMAP_AVAIL_NONE": 0.0,
    "DCGM_FI_DEV_GPU_RECOVERY_ACTION": 0.0,
}
# What a DGX H100 actually reports, so eight cards land on plausible buses.
BUS = ["07", "0B", "48", "4C", "88", "8B", "C8", "CB"]
# Fields that exist only because the card has NVLink, dropped whole for a PCIe card.
NVLINK_PREFIXES = ("DCGM_FI_DEV_NVLINK_", "DCGM_FI_PROF_NVLINK_")
# The driver every simulated host reports. They all run the same one, so the whole fleet reads
# as a single version.
DRIVER_VERSION = "610.43.02"

LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\{(.*)\}\s+(-?[\dEe+.\-]+|[A-Za-z]+)\s*$")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


class Line:
    __slots__ = ("name", "labels", "raw_labels", "base", "key", "gpu_key", "sw_key")

    def __init__(self, name, raw_labels, base):
        self.name, self.raw_labels, self.base = name, raw_labels, base
        self.labels = dict(LABEL.findall(raw_labels))
        self.key = f"{name}{{{raw_labels}}}"
        g = self.labels.get("gpu")
        self.gpu_key = (g, self.labels.get("GPU_I_ID", "")) if g is not None else None
        self.sw_key = (self.labels.get("nvswitch"), self.labels.get("nvlink"))


def parse_payload(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            m = LINE.match(raw)
            if not m:
                continue
            try:
                v = float(m.group(3))
            except ValueError:
                v = m.group(3)
            out.append(Line(m.group(1), m.group(2), v))
    return out


def gpu_uuid(host, orig):
    """A stable GPU UUID per host and card, random looking rather than a counter.

    Real NVIDIA UUIDs are not version 4, so this does not pin the version nibble. A real card
    reports something like GPU-3f9c2a71-0b84-6d5e-91c7-a42e08b6d1f3.
    """
    h = hashlib.sha256(f"{host}/{orig}".encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def card_seed(host, gpu, salt=0):
    """A well spread seed per card.

    The old one was host*17+gpu, which put consecutive cards a ninety-seventh of a cycle apart
    in phase. Eight cards on a baseboard then moved as one, and a table of the fleet showed the
    same number on every row.
    """
    h = hashlib.sha256(f"{host}/{salt}/gpu{gpu}".encode()).hexdigest()
    return int(h[:8], 16)


def load_at(profile, t, seed):
    """0..1 load for one card. Job shaped rather than noisy, so the charts read like real work.

    Each card gets its own level, its own phase and its own stall timing. A real fleet does not
    run every card at the same number: within one job the cards sit at different levels, and the
    straggler is usually the interesting one. The constants come from the seed, so they hold for
    the card's lifetime, and only the jitter moves with time.
    """
    fixed = random.Random(seed)
    phase = fixed.random() * 6.283
    drift = fixed.random() * 6.283
    jitter = random.Random(int(t / 30) * 1000 + seed)
    if profile == "idle":
        level = 0.005 + fixed.random() * 0.05
        return max(0.0, min(1.0, level + 0.015 * math.sin(t / 700.0 + phase)
                            + jitter.uniform(-.006, .006)))
    if profile == "bursty":
        on = ((t / 60.0) + phase * 4) % 23.0 < 14.0
        level = (0.62 + fixed.random() * 0.36) if on else (0.02 + fixed.random() * 0.09)
        return max(0.0, min(1.0, level + 0.06 * math.sin(t / 150.0 + drift)
                            + jitter.uniform(-.03, .03)))
    level = 0.48 + fixed.random() * 0.49
    base = level + 0.09 * math.sin(t / 900.0 + phase)
    if ((t / 60.0) + drift * 5) % 31.0 < 2.0:
        base *= 0.3                                       # checkpoint stall
    return max(0.0, min(1.0, base + jitter.uniform(-.04, .04)))


def norm(load, floor):
    """Scale factor that is 1.0 at the reference load, so the shipped value is the mid point."""
    return (floor + (1.0 - floor) * load) / (floor + (1.0 - floor) * REF)


# The DEV fields spell these Hostname, UUID, modelName and pci_bus_id. The PROF per-link fields
# spell them hostname, gpu_uuid, model_name and pci_bus_id, so both casings have to be rewritten
# or those lines keep reporting the fixture's own card on every simulated host.
CASED = {
    "host": r"(Hostname|hostname)",
    "uuid": r"(UUID|gpu_uuid)",
    "model": r"(modelName|model_name)",
    "bus": r"(pci_bus_id|pciBusId)",
}


def put_cased(raw, group, value):
    return re.sub(rf'(?<![A-Za-z0-9_]){CASED[group]}="[^"]*"',
                  lambda m: f'{m.group(1)}="{value}"', raw)


def put_label(raw, key, value):
    return re.sub(rf'(?<![A-Za-z0-9_]){key}="[^"]*"', f'{key}="{value}"', raw)


def gen_value(name, base, spec):
    """The fixture's value, retuned for this generation."""
    if not isinstance(base, float):
        return base
    if name in HEALTHY:
        return HEALTHY[name]
    key = LIMIT_FIELDS.get(name)
    if key:
        return float(spec[key])
    key = SCALE_FIELDS.get(name)
    if key:
        return base * spec[key] / REF_LIMITS[key]
    return base


def build_machine(name, gen, gpus, mig, lines):
    """Build one machine out of the fixture: `gpus` cards of one model, plus the NVSwitch chips
    that model's baseboard really carries.

    The fixture's GPU 0 is its richest personality, so it is the card template. MIG comes from
    GPU 2, the only partitioned card in there, and the lowercase-label per-link profiling fields
    from GPU 3. The other personalities are dropped: they exist to vary label vocabulary, not to
    describe different hardware.
    """
    spec = GEN[gen]
    card = [ln for ln in lines if ln.labels.get("gpu") == "0" and "GPU_I_ID" not in ln.labels]
    # GPU 0 and GPU 1 are both V100s with the same label vocabulary, and a few fields sit only
    # on GPU 1, XID_ERRORS_TOTAL among them. Taking the union gives every card the full field
    # set, and keying on the field name keeps a field that is on both from arriving twice.
    have = {ln.name for ln in card}
    card += [ln for ln in lines
             if ln.labels.get("gpu") == "1" and "GPU_I_ID" not in ln.labels
             and ln.name not in have]
    mig_tpl = [ln for ln in lines if ln.labels.get("GPU_I_ID")]
    per_link = [ln for ln in lines if "gpu_uuid" in ln.labels]
    switch = [ln for ln in lines if ln.labels.get("nvswitch") == "nvswitch8"]
    out = []

    def emit(ln, ident, value=None):
        raw = ln.raw_labels
        for group, v in ident.items():
            raw = put_cased(raw, group, v) if group in CASED else put_label(raw, group, v)
        # A real fleet shares no GPU UUID between hosts, and shares every bus and switch id,
        # because those are slots rather than parts. Every row of a host gets the same fabric
        # cluster, per-link and MIG rows included, which the fixture leaves at all zeros.
        raw = re.sub(r'FABRIC_CLUSTER_UUID="([0-9a-f-]+)"',
                     lambda m: f'FABRIC_CLUSTER_UUID="{gpu_uuid(name, "fabric")}"', raw)
        # A stock exporter puts the driver version on every series, switch ports included. The
        # fixture predates that label.
        if not re.search(r'(^|,)DCGM_FI_(SYSTEM_)?DRIVER_VERSION=', raw):
            raw += ("," if raw else "") + f'DCGM_FI_DRIVER_VERSION="{DRIVER_VERSION}"'
        out.append(Line(ln.name, raw, gen_value(ln.name, ln.base, spec) if value is None
                        else value))

    for i in range(gpus):
        ident = {"host": name, "model": spec["model"], "uuid": f"GPU-{gpu_uuid(name, f'gpu{i}')}",
                 "bus": f"00000000:{BUS[i % len(BUS)]}:00.0", "gpu": str(i),
                 "device": f"nvidia{i}"}
        for ln in card:
            if spec["links"] == 0 and ln.name.startswith(NVLINK_PREFIXES):
                continue
            emit(ln, ident)
        if spec["links"]:
            for ln in per_link:
                emit(ln, ident)
        # One partitioned card per machine is what a shop offering multi-tenancy actually runs.
        # The fixture's slices are 1g.5gb, which only exists on a 40 GB A100. A one-slice
        # profile holds an eighth of the card's memory, rounded up as NVIDIA names them.
        if mig and i == 0:
            slice_gb = math.ceil(spec["fb"] / 8 / 1024)
            for ln in mig_tpl:
                emit(ln, dict(ident, GPU_I_PROFILE=f"1g.{slice_gb}gb"))

    for s in range(spec["switches"]):
        ident = {"host": name, "nvswitch": f"nvswitch{s}"}
        for ln in switch:
            value = None
            if ln.name == "DCGM_FI_DEV_NVSWITCH_PHYSICAL_ID":
                value = float(s)
            elif ln.name == "DCGM_FI_DEV_NVSWITCH_LINK_REMOTE_PCIE_BUS":
                # The parser finds the card at the other end of a port by this bus number, in
                # decimal, and that is where the port's bandwidth figure comes from. Renumbering
                # the cards without renumbering the ports pointed every port at a card that was
                # not there, so the fabric table showed no bandwidth at all.
                port = int(ln.labels.get("nvlink", "0"))
                value = float(int(BUS[((s * 3 + port) % gpus) % len(BUS)], 16))
            emit(ln, ident, value)
    return out


class FakeHost:
    """One simulated GPU server: its machine, built from the fixture, plus its counters."""

    def __init__(self, name, gen, profile, gpus, mig, lines, seed,
                 faults=None, down_link=None):
        self.name, self.gen, self.profile, self.seed = name, gen, profile, seed
        self.gpus, self.mig = gpus, mig
        self.faults = dict(faults or {})
        self.down_link = down_link
        self.started = time.time()
        self.lines = build_machine(name, gen, gpus, mig, lines)
        # Every counter starts at zero rather than at the fixture's lifetime value. The simulator
        # gets restarted, and a counter that comes back at 45 TB reads to Prometheus as a reset
        # followed by the whole lifetime arriving at once: a 200 GB/s spike on every switch port
        # and a burst of errors on every card. The fixture's timers were absurd anyway,
        # TOTAL_APP_CLOCKS_VIOLATION ships at ninety-four years of throttling.
        self.acc = {ln.key: 0.0 for ln in self.lines if ln.name in COUNTER}
        self.lock = threading.Lock()

    def port_card(self, labels):
        """The card at the other end of a switch port, the one its remote bus number points at."""
        sw = int(labels["nvswitch"].removeprefix("nvswitch") or 0)
        return str((sw * 3 + int(labels.get("nvlink", "0"))) % self.gpus)

    def lost_cards(self, t):
        return {g for g, kind in self.faults.items() if kind == "lost" and t - self.started >= LOST_AFTER}

    def gpu_load(self, labels, t):
        gpu = labels.get("gpu")
        if gpu is None and "nvswitch" in labels:
            # A switch line has no gpu label. Defaulting to card 0 put the same traffic on every
            # port of a host. A port carries the traffic of the card at its other end, so ports
            # now differ the way cards do.
            gpu = self.port_card(labels)
        return load_at(self.profile, t, card_seed(self.name, gpu or "0", self.seed))

    def port_share(self, labels):
        """How much of its card's traffic one port carries. Two ports wired to the same card
        should not report identical numbers either."""
        key = f"{labels.get('nvswitch', '')}/{labels.get('nvlink', '')}"
        return 0.55 + 0.45 * random.Random(card_seed(self.name, key, self.seed)).random()

    def power_w(self, load, fault):
        """Board power in watts, as a share of this card's own limit.

        About 12% idle and just under the cap at full load. Real cards are power capped, so a
        sustained reading above the limit cannot happen. The old model scaled the fixture's
        reading, which it took as the value at 60% load, so busy cards ran at up to 107% of
        their limit. A hot card draws a little more, from leakage, and is still capped.
        """
        frac = 0.12 + 0.85 * load + (0.03 if fault == "thermal" else 0.0)
        return float(GEN[self.gen]["power"]) * min(frac, 0.995)

    def mem_share(self, labels, t):
        """How much of its own memory this card's job is holding, 0 to 1.

        A fraction of the card's own total, not of whatever the fixture shipped. Scaling the
        used figure and the total by the same generation factor, which is what the limits pass
        does, left every card in the fleet reporting an identical memory utilization.
        """
        card = labels.get("gpu", "0")
        if labels.get("GPU_I_ID"):
            card += "/" + labels["GPU_I_ID"]        # each MIG instance runs its own job
        r = random.Random(card_seed(self.name, card, self.seed) ^ 0x5EED)
        base = 0.18 + r.random() * 0.74
        return max(0.03, min(0.96, base + 0.03 * math.sin(t / 1200.0 + r.random() * 6.283)))

    def link_skew(self, labels, direction, t):
        """One direction of a card's NVLink traffic, relative to the card's NVLink rate.

        Each direction drifts by a few percent on its own, so a card never reads the same both
        ways. Receive also sits 3% under transmit, because the drift alone averages out across a
        fleet and the two fleet totals would draw as one line.
        """
        if direction == "both":
            return self.link_skew(labels, "tx", t) + self.link_skew(labels, "rx", t)
        r = random.Random(card_seed(self.name, labels.get("gpu", "0"), direction))
        base = 1.0 if direction == "tx" else 0.97
        return base + 0.04 * math.sin(t / 300.0 + r.random() * 6.283)

    def fault_for(self, labels):
        return self.faults.get(labels.get("gpu", ""))

    def is_down(self, ln, lost):
        """A port is down when it is the configured down link, or when its card was lost."""
        d = self.down_link
        if d and ln.sw_key[0] == d[0] and ln.sw_key[1] == d[1]:
            return True
        return bool(lost) and "nvswitch" in ln.labels and "nvlink" in ln.labels and self.port_card(ln.labels) in lost

    def tick(self, dt, t):
        lost = self.lost_cards(t)
        with self.lock:
            for ln in self.lines:
                cls = COUNTER.get(ln.name)
                if not cls or ln.labels.get("gpu") in lost:
                    continue
                kind, scales = cls
                fault = self.fault_for(ln.labels)
                rate = COUNTER_RATES[kind]
                if fault and kind in FAULT_RATES.get(fault, {}):
                    rate = FAULT_RATES[fault][kind]
                if kind == "swlink" and self.is_down(ln, lost):
                    rate = FAULT_RATES["downlink"]["swlink"]
                if kind == "swbytes" and self.is_down(ln, lost):
                    rate = 0.0                            # a port that is down carries nothing
                if rate == 0.0:
                    continue
                load = self.gpu_load(ln.labels, t)
                if kind == "energy":
                    # The counter integrates the same power the gauge reports, so Energy over a
                    # period agrees with Total Power. A flat rate made a B200 and an L40S burn
                    # energy at the same pace.
                    rate = self.power_w(load, fault) * 1000.0      # millijoules per second
                elif kind == "us_idle":
                    rate *= (1.0 - load)
                elif scales:
                    rate *= (0.25 + 0.75 * load)
                if kind == "swbytes":
                    rate *= self.port_share(ln.labels)
                self.acc[ln.key] += rate * dt

    def render(self):
        t = time.time()
        out = []
        gpu_vals = {}

        def put(ln, v):
            if ln.gpu_key is not None:
                gpu_vals.setdefault(ln.gpu_key, {})[ln.name] = v
            out.append((ln, v))

        lost = self.lost_cards(t)
        with self.lock:
            for ln in self.lines:
                n, b = ln.name, ln.base
                if ln.labels.get("gpu") in lost:
                    continue                # a card that fell off the bus sends nothing at all
                if not isinstance(b, float):
                    put(ln, b)
                    continue
                load = self.gpu_load(ln.labels, t)
                fault = self.fault_for(ln.labels)
                down = self.is_down(ln, lost)

                if n in COUNTER:
                    v = self.acc[ln.key]
                    # Error counters are whole events. Energy and the timers are not.
                    put(ln, math.floor(v) if COUNTER[n][0] in COUNT_KINDS else v)
                elif n == "DCGM_FI_DEV_FB_USED_RATIO":
                    # Has to agree with FB_USED, or a card reports one memory figure in the
                    # ratio field and a different one in the bytes.
                    put(ln, self.mem_share(ln.labels, t))
                elif n in RATIO or n in PERCENT:
                    # Read the shipped value as the reading at full load and scale down with it.
                    # Scaling around a mid point instead pins the busy cards at the cap and
                    # leaves them flat. A field shipped as 0 stays 0, so absent engines stay absent.
                    put(ln, b * load)
                elif n in POWER:
                    put(ln, self.power_w(load, fault))
                elif n in SWITCH_POWER:
                    put(ln, b * norm(load, 0.55))
                elif n in TEMP:
                    # A base of 0 means the sensor reports nothing. Interpolating from an idle
                    # floor would drive it negative, so leave it alone.
                    if b <= 0.0:
                        put(ln, b)
                    else:
                        idle = TEMP[n]
                        put(ln, idle + (b - idle) * load + (19.0 if fault == "thermal" else 0.0))
                elif n in CLOCK:
                    # Boosts with the load but never past the card's own maximum, which the old
                    # scaling did on busy cards. A hot card is held back below that.
                    clk = min(b * norm(load, 0.38), float(GEN[self.gen]["sm"]))
                    put(ln, clk * (0.74 if fault == "thermal" else 1.0))
                elif n in MEM_USED:
                    # A job holds a share of this card's own memory for its lifetime.
                    share = self.mem_share(ln.labels, t)
                    if n == "DCGM_FI_DEV_BAR1_USED":
                        share *= 0.06           # BAR1 is a mapping window, not the working set
                    cap = GEN[self.gen][MEM_USED[n]]
                    # A MIG instance holds only its own slice of the card.
                    prof = re.match(r"\d+g\.(\d+)gb$", ln.labels.get("GPU_I_PROFILE", ""))
                    if prof:
                        cap = int(prof.group(1)) * 1024
                    put(ln, cap * share)
                elif n in MEM_FREE:
                    put(ln, b)                             # fixed up below
                elif n in TRAFFIC:
                    kind, direction, factor = TRAFFIC[n]
                    spec = GEN[self.gen]
                    if kind == "nvlink":
                        full = (spec["links"] * NVLINK_LINK_RATE
                                * self.link_skew(ln.labels, direction, t))
                    else:
                        full = PCIE_RATE[spec["pcie"]]
                    put(ln, full * factor * load)
                elif n in RATE:
                    put(ln, 0.0 if down else b * norm(load, 0.03))
                elif n == "DCGM_FI_DEV_GPU_PSTATE":
                    put(ln, 0.0 if load > 0.45 else (2.0 if load > 0.10 else 8.0))
                elif n == "DCGM_FI_DEV_GPU_RECOVERY_ACTION":
                    put(ln, 1.0 if fault == "xid" else b)
                elif n == "DCGM_FI_DEV_NVSWITCH_LINK_STATUS":
                    put(ln, 0.0 if down else b)
                elif n == "DCGM_FI_DEV_FABRIC_HEALTH_SUMMARY":
                    # 3 is degraded in the connector's table. Of the GPU health statuses, this and
                    # the fabric manager status are the two an earlier connector publishes, so this
                    # is the fault that makes a card show up as unhealthy on any connector version.
                    put(ln, 3.0 if fault == "fabric" else b)
                elif n == "DCGM_EXP_GPU_HEALTH_STATUS":
                    w = ln.labels.get("health_watch")
                    bad = ((fault == "thermal" and w == "THERMAL")
                           or (fault == "ecc" and w == "MEM")
                           or (fault == "xid" and w == "ALL"))
                    put(ln, 20.0 if (bad and fault == "xid") else (10.0 if bad else b))
                elif n in ("DCGM_FI_DEV_ROW_REMAP_CORRECTABLE_TOTAL",
                           "DCGM_FI_DEV_ROW_REMAP_UNCORRECTABLE_TOTAL",
                           "DCGM_FI_DEV_ROW_REMAP_PENDING"):
                    bump = {"DCGM_FI_DEV_ROW_REMAP_CORRECTABLE_TOTAL": 14.0,
                            "DCGM_FI_DEV_ROW_REMAP_UNCORRECTABLE_TOTAL": 3.0,
                            "DCGM_FI_DEV_ROW_REMAP_PENDING": 1.0}[n]
                    put(ln, bump if fault == "ecc" else b)
                elif n.startswith("DCGM_FI_DEV_BANK_REMAP_AVAIL_") and fault == "ecc":
                    lvl = n.rsplit("_", 1)[1]
                    put(ln, {"MAX": max(0.0, b - 11), "HIGH": 6.0, "PARTIAL": 3.0,
                             "LOW": 2.0, "NONE": 0.0}.get(lvl, b))
                else:
                    put(ln, b)

        fixed = []
        for ln, v in out:
            if ln.name in MEM_FREE and ln.gpu_key in gpu_vals:
                tot, used, resv = MEM_FREE[ln.name]
                g = gpu_vals[ln.gpu_key]
                if tot in g and used in g:
                    v = max(0.0, g[tot] - g[used] - (g.get(resv, 0.0) if resv else 0.0))
            fixed.append((ln, v))

        # No banner, because a real exporter prints none.
        buf = []
        for ln, v in fixed:
            s = (f"{v:.6g}" if isinstance(v, float) and abs(v) < 1e15
                 else (f"{v:.0f}" if isinstance(v, float) else str(v)))
            buf.append(f"{ln.name}{{{ln.raw_labels}}} {s}")
        return "\n".join(buf) + "\n"


def make_handler(fake):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_error(404)
                return
            # A real exporter takes a few tens of milliseconds to answer, so the agent sees
            # response times like the ones a real fleet gives.
            time.sleep(random.uniform(0.014, 0.052))
            body = fake.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# The fleet: 12 hosts, 96 GPUs, five generations, which is the upper middle of what an
# enterprise AI platform looks like.
#
# The host names are examples. Change them here, in metricshub-resources.yaml and in
# docker-compose.yml to suit the environment. The model is visible in hw.gpu.model, as it would be
# on real hardware.
#
# Five problems across 96 cards, about 5%, so the issue panels have something to say without the
# fleet reading as though it were on fire.
FLEET = [
    # name             gen     profile     gpus  mig    faults            down link
    ("dev-nvidia-02", "h100", "training", 8, False, {}, None),
    ("dev-nvidia-03", "h100", "training", 8, False, {}, None),
    ("dev-nvidia-04", "h100", "training", 8, False, {"2": "thermal"}, None),
    ("dev-nvidia-05", "h100", "bursty", 8, False, {}, None),
    # Card 3 falls off the bus LOST_AFTER seconds after start, and the two switch ports that lead
    # to it go down with it.
    ("dev-nvidia-06", "h200", "training", 8, False, {"3": "lost"}, None),
    ("dev-nvidia-07", "h200", "training", 8, False, {}, None),
    # The down port leads to card 5, so card 5 is the one whose fabric reports degraded.
    ("dev-nvidia-08", "b200", "bursty", 8, False, {"5": "fabric"}, ("nvswitch1", "2")),
    ("dev-nvidia-09", "a100", "training", 8, True, {}, None),
    ("dev-nvidia-10", "a100", "training", 8, False, {"0": "ecc"}, None),
    ("dev-nvidia-11", "a100", "bursty", 8, True, {}, None),
    ("dev-nvidia-12", "a100", "training", 8, False, {"5": "xid"}, None),
    ("dev-nvidia-13", "l40s", "idle", 8, False, {}, None),
]


def parse_fault(s):
    gpu, _, kind = s.partition("=")
    if kind not in FAULT_RATES:
        raise argparse.ArgumentTypeError(f"unknown fault {kind!r}")
    return gpu, kind


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payload", default="payload-canonical.txt")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--host-name", default="dev-nvidia-02",
                    help="value of the Hostname label the exporter reports")
    ap.add_argument("--gen", default="h100", choices=tuple(GEN),
                    help="GPU generation, which sets the model, its limits and the switch count")
    ap.add_argument("--gpus", type=int, default=8, help="cards on the baseboard")
    ap.add_argument("--mig", action="store_true", help="partition the first card into MIG instances")
    ap.add_argument("--profile", default="training", choices=("training", "bursty", "idle"))
    ap.add_argument("--seed", type=int, default=1,
                    help="shifts this server's load cycle so the fleet does not move in step")
    ap.add_argument("--fault", action="append", type=parse_fault, default=[],
                    metavar="GPU=KIND", help="thermal, ecc, xid, pcie, fabric or lost, e.g. --fault 0=thermal")
    ap.add_argument("--down-link", metavar="SWITCH:LINK",
                    help="take one NVSwitch port down, e.g. nvswitch9:4")
    ap.add_argument("--fleet", action="store_true",
                    help="serve the built-in four servers on consecutive ports in one process")
    a = ap.parse_args()

    lines = parse_payload(a.payload)
    down = tuple(a.down_link.split(":", 1)) if a.down_link else None

    if a.fleet:
        hosts = [FakeHost(n, g, p, n_gpu, mig, lines, i + 1, f, d)
                 for i, (n, g, p, n_gpu, mig, f, d) in enumerate(FLEET)]
        ports = [a.port + i for i in range(len(hosts))]
    else:
        hosts = [FakeHost(a.host_name, a.gen, a.profile, a.gpus, a.mig, lines, a.seed,
                          dict(a.fault), down)]
        ports = [a.port]

    def ticker():
        last = time.time()
        while True:
            time.sleep(1.0)
            now = time.time()
            dt, last = now - last, now
            for h in hosts:
                h.tick(dt, now)

    threading.Thread(target=ticker, daemon=True).start()

    for h, port in zip(hosts, ports):
        s = Server((a.bind, port), make_handler(h))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        faults = ",".join(f"{g}={k}" for g, k in sorted(h.faults.items())) or "-"
        down = ":".join(h.down_link) if h.down_link else "-"
        shape = f"{h.gpus}x{h.gen.upper()}" + (" +mig" if h.mig else "")
        print(f"{h.name:14s} {shape:12s} sw={GEN[h.gen]['switches']} "
              f"profile={h.profile:9s} faults={faults:12s} down={down:14s} "
              f"http://{a.bind}:{port}/metrics", flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
