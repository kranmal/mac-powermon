#!/usr/bin/env python3
"""Smoke tests: the power maths, the Low Power Mode commands, a demo render, and
the real sensors on this Mac. Nothing here changes a system setting.

Run:  .venv/bin/python tests/run_tests.py
"""
import os
import sys
import tempfile
import time

out = tempfile.mkdtemp(prefix="powermon-test-")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["POWERMON_DIR"] = out          # never touch the real settings or usage
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import powermon as m  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

failed = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failed.append(name)


def near(a, b):
    return abs(a - b) < 1e-6


app = QApplication([])

# ---- power maths (milliwatts in, watts out) ----
check("s64()", m.s64(18446744073709539706) == -11910 and m.s64(5) == 5 and m.s64(None) == 0)
batt = m.parse_power({"ExternalConnected": False, "CurrentCapacity": 58, "TimeRemaining": 560,
                      "PowerTelemetryData": {"SystemLoad": 11910,
                                             "BatteryPower": 18446744073709539706}}, 88)
check("on battery: nothing from the wall", near(batt["wall"], 0) and near(batt["sys"], 11.91)
      and near(batt["flow"], -11.91) and batt["mins"] == 560, str(batt))
ac = m.parse_power({"ExternalConnected": True, "CurrentCapacity": 80,
                    "PowerTelemetryData": {"SystemLoad": 12000, "SystemPowerIn": 40000,
                                           "AdapterEfficiencyLoss": 4000, "BatteryPower": 28000}}, 88)
check("plugged in: wall = input + measured loss", near(ac["wall"], 44) and near(ac["flow"], 28)
      and not ac["loss_est"], str(ac))
ac2 = m.parse_power({"ExternalConnected": True, "CurrentCapacity": 80,
                     "PowerTelemetryData": {"SystemLoad": 12000, "SystemPowerIn": 40000}}, 80)
check("plugged in: loss estimated when not reported", near(ac2["wall"], 50) and ac2["loss_est"])
old = m.parse_power({"ExternalConnected": False, "CurrentCapacity": 50,
                     "PowerTelemetryData": {"BatteryPower": 18446744073709539706}}, 88)
check("no SystemLoad: falls back to battery flow", near(old["sys"], 11.91))

# ---- Low Power Mode ----
custom = "Battery Power:\n lowpowermode         0\n sleep 1\nAC Power:\n lowpowermode         1\n"
check("parse_lpm()", m.parse_lpm(custom) == {"b": 0, "c": 1} and m.parse_lpm("") == {})
check("saver on", m.saver_command(True, "b", {"b": 0, "c": 1}, {}) == "/usr/bin/pmset -a lowpowermode 1")
check("saver off restores the other source",
      m.saver_command(False, "b", {"b": 1, "c": 1}, {"b": 0, "c": 1})
      == "/usr/bin/pmset -b lowpowermode 0 && /usr/bin/pmset -c lowpowermode 1")
check("saver off always clears the source in use",
      m.saver_command(False, "c", {"b": 1, "c": 1}, {"b": 0, "c": 1})
      == "/usr/bin/pmset -b lowpowermode 0 && /usr/bin/pmset -c lowpowermode 0")
check("saver off with no saved state keeps the other source as it is",
      m.saver_command(False, "c", {"b": 1, "c": 1}, {})
      == "/usr/bin/pmset -b lowpowermode 1 && /usr/bin/pmset -c lowpowermode 0")

# ---- demo render ----
w = m.Powermon({**m.DEFAULTS, "rate": 21.82}, demo=True)
w.show()
for _ in range(3):
    w.sample()
    app.processEvents()
img = w.grab()
img.save(f"{out}/demo.png")
check("renders (demo)", not img.isNull() and (w.width(), w.height()) == (m.W, m.H))
check("demo saver shows a saving", w.eco and near(w.saved_watts(), 3.2))
check("demo writes nothing", not os.path.exists(m.USAGE_FILE) and not os.path.exists(m.CFG_FILE))
check("money()", w.money(0.0123) == "1.2p" and w.money(12.5) == "£12.50", w.money(0.0123))
w.toggle_eco()
check("demo never touches Low Power Mode", not w.eco_busy and not os.path.exists(m.SAVER_FILE))

# ---- real sensors on this machine: must not raise ----
r = m.Powermon(dict(m.DEFAULTS))
r.show()
time.sleep(1.1)
r.sample()
app.processEvents()
p = r.p
check("real power reading", 0 < p["sys"] < 300 and 0 <= p["wall"] < 400, str(p))
check("real Low Power Mode state", all(v in (0, 1) for v in r.lpm.values()), str(r.lpm))
check("usage only counts wall energy", (r.session_kwh > 0) == (p["wall"] > 0), str(r.session_kwh))
r.save()
check("usage saved", os.path.exists(m.USAGE_FILE))
r.grab().save(f"{out}/real.png")
check("real render", os.path.exists(f"{out}/real.png"))

print(f"\nimages in {out}")
print("ALL PASSED" if not failed else "FAILED: " + ", ".join(failed))
sys.exit(1 if failed else 0)
