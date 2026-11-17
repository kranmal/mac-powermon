#!/usr/bin/env python3
"""Mac Powermon: transparent desktop overlay showing electricity use and cost.

macOS only. A port of linux-powermon. Reads the power figures the Mac measures
itself (system load, adapter input, adapter loss, battery flow) from the
AppleSmartBattery registry entry, no sudo needed, and multiplies the energy taken
from the wall by your unit rate (price per kWh).

Drag with left mouse. Right-click for menu (settings, reset, opacity, quit).
Usage totals persist in ~/Library/Application Support/mac-powermon/usage.json
(counts only while running).
"""
import argparse
import json
import math
import os
import plistlib
import re
import subprocess
import sys
import time
from collections import deque
from datetime import date

from PySide6.QtCore import QPoint, QPointF, QProcess, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QLabel, QLineEdit, QMenu, QSystemTrayIcon,
                               QWidget)

HIST = 60  # samples (1/s)
W, H = 420, 282
FONT = "Menlo"
EDGE = 10  # gap to the screen edge on first launch
DATA_DIR = os.environ.get("POWERMON_DIR") or os.path.expanduser(
    "~/Library/Application Support/mac-powermon")
CFG_FILE = os.path.join(DATA_DIR, "config.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
BASE_FILE = os.path.join(DATA_DIR, "baseline.json")   # avg system watts with saver OFF
SAVER_FILE = os.path.join(DATA_DIR, "saver.json")     # Low Power Mode values before saver ON
DEFAULTS = {"rate": 25.0, "currency": "£", "adapter_eff": 88.0}
SUB = {"£": "p", "$": "¢", "€": "c"}   # name of 1/100 of the currency
EST_IDLE, EST_MAX = 6.0, 40.0          # rough model for Macs with no power telemetry
LEVEL_BELOW, LEVEL_TOP = -1, 3         # NSWindow levels: just under normal windows / floating
PMSET = "/usr/bin/pmset"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def s64(v):
    """ioreg prints signed 64-bit numbers as unsigned."""
    v = int(v or 0)
    return v - 2**64 if v >= 2**63 else v


def parse_power(d, eff):
    """Turn one AppleSmartBattery registry dict into watts. All inputs are milliwatts.

    wall = adapter input + adapter loss. On battery the wall figure is 0: that energy
    was paid for (and counted) when the battery was charged.
    """
    t = d.get("PowerTelemetryData") or {}
    plugged = bool(d.get("ExternalConnected"))
    batt = s64(t.get("BatteryPower")) / 1000        # + charging, - discharging
    sysw = s64(t.get("SystemLoad")) / 1000
    if sysw <= 0:                                   # older firmware: only the battery flow
        sysw = max(-batt, 0.0)
    pin = s64(t.get("SystemPowerIn")) / 1000 if plugged else 0.0
    if plugged and pin <= 0:
        pin = sysw + max(batt, 0.0)
    loss = s64(t.get("AdapterEfficiencyLoss")) / 1000 if plugged else 0.0
    loss_est = False
    if plugged and not 0 < loss < 0.5 * pin:        # missing or implausible: use the setting
        loss, loss_est = pin * (100 / eff - 1), True
    mins = d.get("TimeRemaining")
    return {"plugged": plugged, "sys": sysw, "in": pin, "loss": loss, "loss_est": loss_est,
            "flow": pin - sysw if plugged else -sysw,   # into (+) or out of (-) the battery
            "wall": pin + loss, "pct": float(d.get("CurrentCapacity", 0)),
            "mins": mins if isinstance(mins, int) and 0 < mins < 6000 else None,
            "est": False, "battery": True}


def read_power(eff):
    try:
        out = subprocess.run(["ioreg", "-r", "-n", "AppleSmartBattery", "-a"],
                             capture_output=True, timeout=3).stdout
        for d in plistlib.loads(out) if out else []:
            if "CurrentCapacity" in d:
                return parse_power(d, eff)
    except Exception:
        pass
    # No battery (desktop Mac): nothing measures total power without root, so estimate.
    load = min(os.getloadavg()[0] / (os.cpu_count() or 1), 1.0)
    sysw = EST_IDLE + load * (EST_MAX - EST_IDLE)
    return {"plugged": True, "sys": sysw, "in": sysw, "loss": sysw * (100 / eff - 1),
            "loss_est": True, "flow": 0.0, "wall": sysw * 100 / eff, "pct": 0.0,
            "mins": None, "est": True, "battery": False}


def parse_lpm(text):
    """`pmset -g custom` output -> {"b": 0/1, "c": 0/1} for the sources that have the key."""
    out, src = {}, None
    for line in text.splitlines():
        if line.startswith("Battery Power"):
            src = "b"
        elif line.startswith("AC Power"):
            src = "c"
        else:
            m = re.match(r"\s+lowpowermode\s+(\d)", line)
            if m and src:
                out[src] = int(m.group(1))
    return out


def read_lpm():
    try:
        return parse_lpm(subprocess.run([PMSET, "-g", "custom"], capture_output=True,
                                        text=True, timeout=3).stdout)
    except Exception:
        return {}


def saver_command(turn_on, src, lpm, saved):
    """Shell command for the saver button. src is the power source in use ("b" or "c").

    ON sets Low Power Mode for both sources. OFF puts back what was there before,
    except that the source in use always ends up off (otherwise the click does nothing).
    """
    if turn_on:
        return f"{PMSET} -a lowpowermode 1"
    want = {k: 1 if saved.get(k, v) == 1 else 0 for k, v in lpm.items()}
    want[src] = 0
    return " && ".join(f"{PMSET} -{k} lowpowermode {v}" for k, v in sorted(want.items()))


def bolt_icon():
    """Menu bar icon: a lightning bolt, drawn as a template so macOS tints it."""
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("black"))
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in
                             ((26, 3), (9, 25), (20, 25), (16, 41), (35, 18), (23, 18))]))
    p.end()
    pm.setDevicePixelRatio(2)
    ic = QIcon(pm)
    ic.setIsMask(True)
    return ic


def bring_forward(dialog):
    """An app with no Dock icon opens dialogs behind other windows unless asked."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
    dialog.raise_()
    dialog.activateWindow()


class SettingsDialog(QDialog):
    def __init__(self, cfg, first=False):
        super().__init__()
        self.setWindowTitle("Powermon settings")
        form = QFormLayout(self)
        if first:
            form.addRow(QLabel("Welcome! Enter your electricity tariff.\n"
                               "Unit rate is on your bill, e.g. 24.5p per kWh."))
        self.cur = QLineEdit(cfg["currency"])
        self.cur.setMaxLength(3)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.0, 1000.0)
        self.rate.setDecimals(2)
        self.rate.setSuffix(" pence/cents per kWh")
        self.rate.setValue(cfg["rate"])
        self.eff = QDoubleSpinBox()
        self.eff.setRange(50.0, 100.0)
        self.eff.setSuffix(" %")
        self.eff.setValue(cfg["adapter_eff"])
        form.addRow("Currency symbol", self.cur)
        form.addRow("Unit rate", self.rate)
        form.addRow("Charger efficiency (used only\nif the Mac doesn't report it)", self.eff)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self):
        return {"rate": self.rate.value(), "currency": self.cur.text() or "£",
                "adapter_eff": self.eff.value()}

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, lambda: bring_forward(self))


class Powermon(QWidget):
    def __init__(self, cfg, demo=False):
        super().__init__()
        self.demo = demo
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("powermon")
        self.resize(W, H)
        self.cfg = cfg
        self.bg_alpha = int(cfg.get("alpha", 90))
        self.on_top = bool(cfg.get("on_top", False))
        self.hist = deque([0.0] * HIST, maxlen=HIST)
        self.p = read_power(cfg["adapter_eff"])
        self._t = time.monotonic()
        self.session_kwh = 0.0
        self.usage = load_json(USAGE_FILE, {})  # "YYYY-MM-DD" -> wall kWh
        if demo:
            self.usage = {date.today().isoformat(): 0.184, date.today().strftime("%Y-%m-01"): 3.95}
            self.session_kwh = 0.062
        self.base = load_json(BASE_FILE, {"sys": 0.0, "n": 0})  # learned "normal" draw
        self._save_t = time.monotonic()
        self._seen = False
        self._placed = False
        self.btn_rect = QRectF()
        self.eco_rect = QRectF()
        self.lpm = {} if demo else read_lpm()
        self._lpm_t = time.monotonic()
        self.eco_busy = False
        self.eco_msg = ""
        self.eco_proc = None
        self._move_timer = QTimer(self)
        self._move_timer.setSingleShot(True)
        self._move_timer.timeout.connect(self.save_window)
        t = QTimer(self)
        t.timeout.connect(self.sample)
        t.start(1000)
        self.tray = self.make_tray()
        self.sample()

    # ---- window placement (macOS) ----
    def nswindow(self):
        if QGuiApplication.platformName() != "cocoa":
            return None
        try:
            import objc
            return objc.objc_object(c_void_p=int(self.winId())).window()
        except Exception:
            return None

    def apply_native(self):
        win = self.nswindow()
        if win is None:
            return
        win.setLevel_(LEVEL_TOP if self.on_top else LEVEL_BELOW)
        # all Spaces | stays put in Mission Control | skipped by Cmd-` window cycling
        win.setCollectionBehavior_(1 | 16 | 64)
        win.setHidesOnDeactivate_(False)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._placed:
            self.move(self.start_pos())
            self._placed = True
        self.apply_native()

    def start_pos(self):
        """Where it was last dragged to, else the bottom-left corner of the main screen."""
        if "x" in self.cfg and "y" in self.cfg:
            saved = QPoint(int(self.cfg["x"]), int(self.cfg["y"]))
            if any(s.geometry().contains(saved) for s in QGuiApplication.screens()):
                return saved
        scr = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(scr.left() + EDGE, scr.bottom() + 1 - H - EDGE)

    def moveEvent(self, e):
        super().moveEvent(e)
        if self._placed:
            self._move_timer.start(800)

    def save_window(self):
        if self.demo or QGuiApplication.platformName() != "cocoa":
            return
        self.cfg.update(x=self.x(), y=self.y(), alpha=self.bg_alpha, on_top=self.on_top)
        save_json(CFG_FILE, self.cfg)

    # ---- menu bar icon ----
    def make_tray(self):
        """Menu bar bolt: tooltip shows live watts and cost; its menu hides/shows the overlay."""
        if QGuiApplication.platformName() != "cocoa" or not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(bolt_icon(), self)
        tray.setToolTip("Mac Powermon")
        m = QMenu(self)
        self.tray_toggle = QAction("Hide overlay", m)
        self.tray_toggle.triggered.connect(self.toggle_visible)
        m.addAction(self.tray_toggle)
        for label, fn in (("Settings (rate)…", self.settings),
                          ("Low Power Mode: on / off", self.toggle_eco)):
            a = QAction(label, m)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        q = QAction("Quit", m)
        q.triggered.connect(QApplication.quit)
        m.addAction(q)
        tray.setContextMenu(m)
        tray.show()
        return tray

    def toggle_visible(self):
        self.setVisible(not self.isVisible())
        if self.tray:
            self.tray_toggle.setText("Hide overlay" if self.isVisible() else "Show overlay")

    # ---- sampling ----
    @property
    def src(self):
        return "c" if self.p["plugged"] else "b"

    @property
    def eco(self):
        return self.lpm.get(self.src) == 1

    def now_w(self):
        """Headline figure: wall watts when plugged in, the Mac's own draw on battery."""
        return self.p["wall"] if self.p["plugged"] else self.p["sys"]

    def cost_w(self):
        """Wall watts to price: on battery, what it will take to put this back."""
        if self.p["plugged"]:
            return self.p["wall"]
        return self.p["sys"] * 100 / self.cfg["adapter_eff"]

    def sample(self):
        now = time.monotonic()
        dt = min(now - self._t, 5.0)   # never bill a pause (sleep, stalled timer) as usage
        self._t = now
        if self.demo:
            self.p = self.demo_power(now)
            self.lpm = {"b": 1, "c": 1}
            self.base = {"sys": self.p["sys"] + 3.2, "n": 999}
        else:
            self.p = read_power(self.cfg["adapter_eff"])
            if now - self._lpm_t > 5 and not self.eco_busy:
                self.lpm, self._lpm_t = read_lpm(), now
        w = self.now_w()
        if not self._seen:
            self._seen = True
            self.hist.extend(self.demo_power(now - i)["wall"] for i in range(HIST, 0, -1)) \
                if self.demo else self.hist.extend([w] * HIST)
        self.hist.append(w)
        if self.tray:
            self.tray.setToolTip(
                f"Mac Powermon\n{w:.1f} W" + ("" if self.p["plugged"] else " on battery")
                + f" · {self.money(self.cost(self.cost_w() / 1000))}/h"
                + ("\nLow Power Mode on" if self.eco else ""))
        if self.demo:
            self.update()
            return
        if not self.eco and not self.eco_busy:
            n = self.base["n"] + 1  # running mean at first, then ~5 min moving average
            self.base["n"] = n
            self.base["sys"] += max(1 / n, 0.003) * (self.p["sys"] - self.base["sys"])
        kwh = self.p["wall"] * dt / 3.6e6
        self.session_kwh += kwh
        today = date.today().isoformat()
        self.usage[today] = self.usage.get(today, 0.0) + kwh
        if now - self._save_t > 60:
            self.save()
        self.update()

    def demo_power(self, t):
        sysw = 14 + 8 * math.sin(t / 5)
        pin = sysw + 21 + 4 * math.sin(t / 9 + 1)
        return {"plugged": True, "sys": sysw, "in": pin, "loss": pin * 0.11, "loss_est": False,
                "flow": pin - sysw, "wall": pin * 1.11, "pct": 74.0, "mins": 48,
                "est": False, "battery": True}

    def save(self):
        self._save_t = time.monotonic()
        save_json(USAGE_FILE, self.usage)
        save_json(BASE_FILE, self.base)

    def saved_watts(self):
        """Watts saved vs the learned normal draw; None until a baseline exists."""
        if self.base["n"] < 30:
            return None
        return max(0.0, self.base["sys"] - self.p["sys"])

    def month_kwh(self):
        prefix = date.today().strftime("%Y-%m")
        return sum(v for k, v in self.usage.items() if k.startswith(prefix))

    def cost(self, kwh):
        return kwh * self.cfg["rate"] / 100

    def money(self, v):
        """A laptop costs pennies, so small amounts are shown in pence/cents."""
        c = self.cfg["currency"]
        if v < 0.995:
            return f"{v * 100:.1f}{SUB[c]}" if c in SUB else f"{c}{v:.3f}"
        return f"{c}{v:,.2f}" if v < 1000 else f"{c}{v:,.0f}"

    # ---- drawing helpers (same look as sysmon) ----
    def text(self, p, x, y, s, color=QColor("white"), bold=False, size=10,
             align=Qt.AlignLeft, width=0):
        f = QFont(FONT, size)
        f.setBold(bold)
        p.setFont(f)
        r = QRectF(x, y - 14, width or 300, 18)
        p.setPen(QColor(0, 0, 0, 200))
        p.drawText(r.translated(1, 1), align | Qt.AlignVCenter, s)
        p.setPen(color)
        p.drawText(r, align | Qt.AlignVCenter, s)

    def bar(self, p, x, y, w, pct, color):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(x, y, w, 6), 3, 3)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x, y, w * min(max(pct, 0), 100) / 100, 6), 3, 3)

    def spark(self, p, x, y, w, h, data, color, maxv):
        data = list(data)
        n = len(data)
        path = QPainterPath()
        for i, v in enumerate(data):
            px = x + w * i / (n - 1)
            py = y + h - h * min(v / maxv, 1.0)
            path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
        fill = QPainterPath(path)
        fill.lineTo(x + w, y + h)
        fill.lineTo(x, y + h)
        fill.closeSubpath()
        c = QColor(color)
        c.setAlpha(50)
        p.setPen(Qt.NoPen)
        p.fillPath(fill, c)
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def section(self, p, x, y, w, title):
        self.text(p, x, y, title, QColor("#9ad1ff"), bold=True, size=9)
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.drawLine(int(x + len(title) * 6 + 10), int(y - 5), int(x + w), int(y - 5))

    @staticmethod
    def watt_color(w):
        if w < 15:
            return QColor("#5fe3a1")
        if w < 45:
            return QColor("#ffcf5c")
        return QColor("#ff6b6b")

    def row(self, p, x, y, w, label, kwh, cost):
        self.text(p, x, y, label, QColor("#cccccc"), size=9)
        self.text(p, x + 50, y, f"{kwh:7.3f} kWh" if kwh < 100 else f"{kwh:7.1f} kWh",
                  bold=True, size=9)
        self.text(p, 0, y, cost, QColor("#ffcf5c"), bold=True, size=9,
                  align=Qt.AlignRight, width=x + w)

    def button(self, p, rect, label, col, filled=False):
        fill = QColor(col)
        fill.setAlpha(60 if filled else 25)
        p.setPen(QPen(col, 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 8, 8)
        self.text(p, rect.x(), rect.y() + 19, label, col, bold=True, size=9,
                  align=Qt.AlignHCenter, width=rect.width())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        pw = self.p
        now_w = self.now_w()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 12, 18, self.bg_alpha))
        p.drawRoundedRect(QRectF(0, 0, W, self.height()), 14, 14)

        cw = (W - 42) // 2            # column width
        lx, rx = 14, 14 + cw + 14     # left / right column x
        c = self.cfg["currency"]

        y = 26
        self.text(p, 14, y, time.strftime("%H:%M:%S"), size=16, bold=True)
        self.text(p, 0, y, f"{self.cfg['rate']:g}{SUB.get(c, '')}/kWh", QColor("#ffcf5c"),
                  bold=True, size=10, align=Qt.AlignHCenter, width=W)
        self.text(p, 0, y, time.strftime("%a %d %b"), QColor("#cccccc"),
                  align=Qt.AlignRight, width=W - 14)

        # left: power now
        y = 56
        est = "*" if pw["est"] else ""
        left = f" {pw['mins'] // 60}:{pw['mins'] % 60:02d} LEFT" if pw["mins"] else ""
        self.section(p, lx, y, cw, "FROM THE WALL" + est if pw["plugged"] else "ON BATTERY" + left)
        col = self.watt_color(now_w)
        self.text(p, lx, y + 30, f"{now_w:4.1f} W", col, bold=True, size=20)
        self.text(p, 0, y + 30, f"{self.money(self.cost(self.cost_w() / 1000))}/h"
                  + ("" if pw["plugged"] else "*"), QColor("#ffcf5c"),
                  bold=True, size=12, align=Qt.AlignRight, width=lx + cw)
        self.spark(p, lx, y + 44, cw, 38, self.hist, col, max(max(self.hist), 20.0))

        # right: breakdown
        self.section(p, rx, y, cw, "BREAKDOWN *=est")
        flow = pw["flow"]
        flow_label = "Charging" if flow >= 0.05 else "From batt" if flow <= -0.05 else "Battery"
        rows = [("Mac" + est, pw["sys"], QColor("#5fb3ff")),
                (flow_label, abs(flow), QColor("#c58bff")),
                ("Charger" + ("*" if pw["loss_est"] else ""), pw["loss"], QColor("#ff9f5c"))]
        scale = max(pw["wall"], pw["sys"])
        ry = y + 26
        for label, w, rc in rows:
            self.text(p, rx, ry, label, QColor("#cccccc"), size=9)
            self.bar(p, rx + 66, ry - 8, cw - 66 - 52, 100 * w / scale if scale else 0, rc)
            self.text(p, 0, ry, f"{w:4.1f} W", rc, bold=True, size=9,
                      align=Qt.AlignRight, width=rx + cw)
            ry += 19
        if pw["battery"]:
            green = QColor("#5fe3a1")
            self.text(p, rx, ry, "Level", QColor("#cccccc"), size=9)
            self.bar(p, rx + 66, ry - 8, cw - 66 - 52, pw["pct"], green)
            self.text(p, 0, ry, f"{pw['pct']:.0f}%", green, bold=True, size=9,
                      align=Qt.AlignRight, width=rx + cw)

        # usage / projection
        y = 156
        self.section(p, lx, y, cw, "USAGE")
        self.section(p, rx, y, cw, "IF LEFT ON")
        for i, (label, kwh) in enumerate((("Today", self.usage.get(date.today().isoformat(), 0.0)),
                                          ("Session", self.session_kwh),
                                          ("Month", self.month_kwh()))):
            self.row(p, lx, y + 24 + i * 21, cw, label, kwh, self.money(self.cost(kwh)))
        for i, (label, hours) in enumerate((("Day", 24), ("Month", 24 * 30), ("Year", 24 * 365))):
            kwh = self.cost_w() * hours / 1000
            self.row(p, rx, y + 24 + i * 21, cw, label, kwh, self.money(self.cost(kwh)))

        # buttons
        by = 240
        self.eco_rect = QRectF(14, by, 284, 28)
        if self.eco_busy:
            label = "WAITING FOR PASSWORD…"
        elif self.eco_msg:
            label = self.eco_msg
        else:
            sv = self.saved_watts()
            if not self.eco:
                label = "LOW POWER MODE: OFF (click = on)"
            elif sv is None:
                label = "LOW POWER ON: no baseline (click=off)"
            else:
                label = f"LOW POWER ON: saving ~{sv:.1f} W (click=off)"
        self.button(p, self.eco_rect, label,
                    QColor("#5fe3a1") if self.eco else QColor("#ffcf5c"), self.eco)
        self.btn_rect = QRectF(14 + 284 + 12, by, W - 28 - 284 - 12, 28)
        self.button(p, self.btn_rect, "SETTINGS", QColor("#9ad1ff"))

    # ---- interaction ----
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.btn_rect.contains(e.position()):
            self.settings()
        elif self.eco_rect.contains(e.position()):
            self.toggle_eco()
        elif self.windowHandle():
            self.windowHandle().startSystemMove()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        s = QAction("Settings (rate)…", m)
        s.triggered.connect(self.settings)
        m.addAction(s)
        eco = QAction("Low Power Mode: turn " + ("off" if self.eco else "on"), m)
        eco.triggered.connect(self.toggle_eco)
        m.addAction(eco)
        r = QAction("Reset today's + session totals", m)
        r.triggered.connect(self.reset)
        m.addAction(r)
        if self.tray:
            h = QAction("Hide (bring back from the menu bar bolt)", m)
            h.triggered.connect(self.toggle_visible)
            m.addAction(h)
        m.addSeparator()
        for label, a in (("Opacity: clear", 0), ("Opacity: light", 60),
                         ("Opacity: medium", 130), ("Opacity: dark", 200)):
            act = QAction(label, m)
            act.triggered.connect(lambda _=False, a=a: self.set_alpha(a))
            m.addAction(act)
        top = QAction("Float on top", m)
        top.setCheckable(True)
        top.setChecked(self.on_top)
        top.triggered.connect(self.set_on_top)
        m.addAction(top)
        m.addSeparator()
        q = QAction("Quit", m)
        q.triggered.connect(QApplication.quit)
        m.addAction(q)
        m.exec(e.globalPos())

    def flash(self, msg):
        self.eco_msg = msg
        QTimer.singleShot(4000, self.clear_msg)
        self.update()

    def toggle_eco(self):
        """Switch Low Power Mode. pmset needs root, so macOS asks for an admin password."""
        if self.eco_busy or self.demo:
            return
        self.lpm = read_lpm()
        if self.src not in self.lpm:
            self.flash("Low Power Mode not available here")
            return
        turn_on = not self.eco
        if turn_on:
            save_json(SAVER_FILE, self.lpm)   # what to put back on the next click
        cmd = saver_command(turn_on, self.src, self.lpm, load_json(SAVER_FILE, {}))
        self.eco_busy, self.eco_msg = True, ""
        self.eco_proc = QProcess(self)
        self.eco_proc.finished.connect(lambda code, _s, on=turn_on: self.eco_done(code, on))
        self.eco_proc.start("/usr/bin/osascript", [
            "-e", f'do shell script "{cmd}" with administrator privileges '
                  'with prompt "Mac Powermon wants to switch Low Power Mode."'])
        self.update()

    def eco_done(self, code, turned_on):
        self.eco_busy = False
        self.lpm, self._lpm_t = read_lpm(), time.monotonic()
        if code != 0:
            self.flash("cancelled / not authorised")
        elif not turned_on:
            try:
                os.remove(SAVER_FILE)
            except OSError:
                pass
        self.update()

    def clear_msg(self):
        self.eco_msg = ""
        self.update()

    def settings(self):
        d = SettingsDialog(self.cfg)
        if d.exec():
            self.cfg.update(d.values())
            if not self.demo:
                save_json(CFG_FILE, self.cfg)
            self.update()

    def reset(self):
        self.usage.pop(date.today().isoformat(), None)
        self.session_kwh = 0.0
        if not self.demo:
            self.save()
        self.update()

    def set_alpha(self, a):
        self.bg_alpha = a
        self.save_window()
        self.update()

    def set_on_top(self, on):
        self.on_top = bool(on)
        self.apply_native()
        self.save_window()


def main():
    if sys.platform != "darwin":
        sys.exit("Mac Powermon only runs on macOS. For Linux see kranmal/linux-powermon.")
    ap = argparse.ArgumentParser(description="Transparent electricity-cost overlay (macOS).")
    ap.add_argument("--demo", action="store_true", help="fake data, nothing is saved (screenshots)")
    ap.add_argument("--setup", action="store_true", help="re-ask tariff settings at start")
    args, qt_args = ap.parse_known_args()
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("powermon")
    app.setQuitOnLastWindowClosed(False)  # keep running in the menu bar when the overlay is hidden
    try:   # no Dock icon, no app-switcher entry
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception:
        pass
    cfg = {**DEFAULTS, **load_json(CFG_FILE, {})}
    if args.demo:
        cfg = {**DEFAULTS, "rate": 21.82}
    elif args.setup or not os.path.exists(CFG_FILE):
        d = SettingsDialog(cfg, first=True)
        if d.exec():
            cfg.update(d.values())
            save_json(CFG_FILE, cfg)
        elif not os.path.exists(CFG_FILE):
            sys.exit("Tariff not set; cancelled.")
    w = Powermon(cfg, demo=args.demo)
    if not args.demo:
        app.aboutToQuit.connect(w.save)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
