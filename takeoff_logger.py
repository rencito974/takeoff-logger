r"""
takeoff_logger.py - records takeoff runs in Microsoft Flight Simulator (2020/2024) via SimConnect
===============================================================================================
Built for an investigation of how aircraft mass affects the takeoff ground roll (the IA).
EE runs are recorded with ee_logger.py, which builds on this script.

SETUP (Windows, 64-bit Python 3):
    py -m pip install SimConnect

USE (load into a flight and sit in the cockpit FIRST, then run):
    py takeoff_logger.py --check      live readout, to test the connection
    py takeoff_logger.py              record takeoff runs  (Ctrl+C to quit)
    py takeoff_logger.py --dll "C:\path\to\SimConnect.dll"    use a different SimConnect.dll
    py takeoff_logger.py --out other_folder    save the CSVs somewhere other than the IA folder

HOW A RUN IS DETECTED
    1. Stop on the runway, hold the brakes, FULL throttle   -> "ARMED"
       (the ARMED line shows mass, flap angle, trim, wind, temperature, elevation and mixture -
        check them before you go)
    2. Release the brakes: recording starts once ground speed passes 1 kt
    3. Take off: the run is saved once the plane has stayed airborne for 0.5 s
       (touching back down before that counts as a bounce and the run carries on)
    Pulling the throttle below 80 % during the roll cancels the run.
    Don't pause the sim during a run.

OUTPUT (folder "IA" next to this script)
    run_001.csv, run_002.csv ...   every simulator frame of each run
    runs_summary.csv               one row per run: mass, ground roll, liftoff values, conditions
    Tip: if Excel puts everything in one column, use Data > From Text/CSV, or open it in Google Sheets.
"""

import argparse
import csv
import logging
import math
import queue
import sys
import time
from ctypes import POINTER, Structure, byref, c_double, cast
from ctypes.wintypes import DWORD
from datetime import datetime
from pathlib import Path
from statistics import mean

try:
    from SimConnect import SimConnect
    from SimConnect.Constants import SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_UNUSED
    from SimConnect.Enum import (
        SIMCONNECT_DATA_REQUEST_FLAG,
        SIMCONNECT_DATATYPE,
        SIMCONNECT_EXCEPTION,
        SIMCONNECT_PERIOD,
        SIMCONNECT_RECV_ID,
        SIMCONNECT_RECV_SIMOBJECT_DATA,
    )
except ImportError:
    sys.exit("The SimConnect package isn't installed. Run:  py -m pip install SimConnect")

# ------------------------------------------------------------------ settings
ARM_THROTTLE_PCT = 90.0     # throttle (while stopped) that arms a run
START_SPEED_KT = 1.0        # ground speed that counts as "brakes released"
ABORT_THROTTLE_PCT = 80.0   # throttle below this during the roll cancels the run
AIRBORNE_CONFIRM_S = 0.5    # how long the plane must stay airborne to count as a liftoff
MAX_ROLL_S = 120.0          # give up if there's no liftoff after this long
EXPECTED_FLAPS_DEG = 10.0   # warn at arming if the flaps aren't here

# Sim variables streamed once per simulator frame: (column name, SimConnect variable, units)
VARS = [
    ("abs_time_s",    "ABSOLUTE TIME",                         "Seconds"),
    ("on_ground",     "SIM ON GROUND",                         "Bool"),
    ("lat_deg",       "PLANE LATITUDE",                        "Degrees"),
    ("lon_deg",       "PLANE LONGITUDE",                       "Degrees"),
    ("ground_alt_m",  "GROUND ALTITUDE",                       "Meters"),
    ("gs_kt",         "GROUND VELOCITY",                       "Knots"),
    ("ias_kt",        "AIRSPEED INDICATED",                    "Knots"),
    ("tas_kt",        "AIRSPEED TRUE",                         "Knots"),
    ("throttle_pct",  "GENERAL ENG THROTTLE LEVER POSITION:1", "Percent"),
    ("rpm",           "GENERAL ENG RPM:1",                     "Rpm"),
    ("mixture_pct",   "GENERAL ENG MIXTURE LEVER POSITION:1",  "Percent"),
    ("pitch_raw_deg", "PLANE PITCH DEGREES",                   "Degrees"),
    ("aoa_deg",       "INCIDENCE ALPHA",                       "Degrees"),
    ("flaps_index",   "FLAPS HANDLE INDEX",                    "Number"),
    ("flaps_deg",     "TRAILING EDGE FLAPS LEFT ANGLE",        "Degrees"),
    ("trim_raw",      "ELEVATOR TRIM PCT",                     "Percent Over 100"),
    ("weight_lb",     "TOTAL WEIGHT",                          "Pounds"),
    ("temp_c",        "AMBIENT TEMPERATURE",                   "Celsius"),
    ("pressure_inhg", "AMBIENT PRESSURE",                      "inHg"),
    ("wind_kt",       "AMBIENT WIND VELOCITY",                 "Knots"),
]
COLUMNS = [column for column, _, _ in VARS]

FRAME_COLUMNS = ["t_s", "dist_m", "on_ground", "gs_kt", "ias_kt", "tas_kt", "throttle_pct", "rpm",
                 "pitch_deg", "aoa_deg", "flaps_index", "flaps_deg", "trim_pct", "weight_lb", "temp_c", "pressure_inhg",
                 "wind_kt", "lat_deg", "lon_deg", "abs_time_s",
                 "ground_alt_m", "mixture_pct"]     # added later: kept at the end so older files line up

SUMMARY_COLUMNS = ["run", "recorded_at", "mass_lb", "mass_kg", "ground_roll_m", "ground_roll_check_m",
                   "roll_time_s", "liftoff_ias_kt", "liftoff_tas_kt", "liftoff_gs_kt", "liftoff_aoa_deg",
                   "liftoff_pitch_deg", "flaps_deg", "trim_pct", "max_rpm", "temp_c", "pressure_hpa",
                   "air_density_kg_m3", "max_wind_kt", "bounced", "flaps_moved", "time_source", "file",
                   "elevation_m", "mixture_pct"]    # added later, same reason

KT_TO_MS = 0.514444         # knots -> m/s
LB_TO_KG = 0.45359237       # pounds -> kg
INHG_TO_PA = 3386.389       # inches of mercury -> pascals
R_AIR = 287.05              # specific gas constant of dry air, J/(kg K)

DEFINITION_ID = 900         # our own IDs for the SimConnect data request
REQUEST_ID = 900


def ground_distance_m(lat1, lon1, lat2, lon2):
    """Distance in metres between two nearby points (flat-earth approximation on the
    WGS84 ellipsoid - plenty accurate over a few kilometres)."""
    a, e2 = 6378137.0, 6.69437999014e-3
    phi = math.radians((lat1 + lat2) / 2)
    w = 1 - e2 * math.sin(phi) ** 2
    r_north = a * (1 - e2) / w ** 1.5    # meridian radius of curvature
    r_east = a / math.sqrt(w)            # prime-vertical radius of curvature
    dy = math.radians(lat2 - lat1) * r_north
    dx = math.radians(lon2 - lon1) * r_east * math.cos(phi)
    return math.hypot(dx, dy)


class _RecvException(Structure):
    # SIMCONNECT_RECV_EXCEPTION exactly as laid out in SimConnect.h
    _fields_ = [("dwSize", DWORD), ("dwVersion", DWORD), ("dwID", DWORD),
                ("dwException", DWORD), ("dwSendID", DWORD), ("dwIndex", DWORD)]


class FrameFeed(SimConnect):
    """SimConnect connection that delivers every variable in VARS once per simulator frame."""

    def __init__(self, frame_queue, dll_path=None):
        self.frame_queue = frame_queue
        self.var_packets = {}            # packet id -> variable name, to report rejected variables
        if dll_path:
            super().__init__(auto_connect=True, library_path=dll_path)
        else:
            super().__init__(auto_connect=True)

    def start_stream(self):
        for _, name, units in VARS:
            self.dll.AddToDataDefinition(
                self.hSimConnect, DEFINITION_ID, name.encode(), units.encode(),
                SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64, 0, SIMCONNECT_UNUSED)
            self._remember_packet(f"the variable '{name}'")
        self.dll.RequestDataOnSimObject(
            self.hSimConnect, REQUEST_ID, DEFINITION_ID, SIMCONNECT_OBJECT_ID_USER,
            SIMCONNECT_PERIOD.SIMCONNECT_PERIOD_SIM_FRAME,
            SIMCONNECT_DATA_REQUEST_FLAG.SIMCONNECT_DATA_REQUEST_FLAG_DEFAULT, 0, 0, 0)
        self._remember_packet("the per-frame data request")

    def _remember_packet(self, label):
        packet = DWORD(0)
        self.dll.GetLastSentPacketID(self.hSimConnect, byref(packet))
        self.var_packets[packet.value] = label

    def my_dispatch_proc(self, pData, cbData, pContext):
        # Called on the library's background thread for every message the sim sends.
        try:
            msg_id = pData.contents.dwID
            if msg_id == SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                msg = cast(pData, POINTER(SIMCONNECT_RECV_SIMOBJECT_DATA)).contents
                if msg.dwRequestID == REQUEST_ID:
                    n = msg.dwDefineCount
                    values = tuple(cast(msg.dwData, POINTER(c_double * n)).contents) if n else ()
                    self.frame_queue.put((time.perf_counter(), n, values))
            elif msg_id == SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION:
                exc = cast(pData, POINTER(_RecvException)).contents
                name = self.var_packets.get(exc.dwSendID)
                if name is not None:           # only report problems with OUR requests
                    try:
                        reason = SIMCONNECT_EXCEPTION(exc.dwException).name
                    except ValueError:
                        reason = f"code {exc.dwException}"
                    print(f"\nSimConnect rejected {name} ({reason}).")
            elif msg_id in (SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_OPEN,
                            SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_QUIT,
                            SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EVENT):
                super().my_dispatch_proc(pData, cbData, pContext)   # connection bookkeeping
            # every other message type is ignored
        except Exception as err:               # never let an error kill the background thread
            print(f"\nError while reading SimConnect data: {err}")

    def close(self):
        if getattr(self, "timerThread", None) is not None:
            self.exit()


def frames(feed, frame_queue):
    """Yield (pc_time, values) for every simulator frame until the sim closes."""
    waiting_note = False
    while True:
        try:
            t, n, values = frame_queue.get(timeout=1.0)
        except queue.Empty:
            if feed.quit:
                print("\nFlight Simulator closed the connection.")
                return
            if not waiting_note:
                print("Waiting for data... (be in the cockpit, sim unpaused; if nothing comes, "
                      "restart this script once you're in the flight)")
                waiting_note = True
            continue
        if n != len(VARS):
            print(f"\nReceived {n} values instead of {len(VARS)}: a variable was rejected (see above).")
            return
        waiting_note = False
        f = dict(zip(COLUMNS, values))
        f["pitch_deg"] = -f.pop("pitch_raw_deg")   # SimConnect's pitch is positive nose-DOWN; flip it
        f["trim_pct"] = 100 * f.pop("trim_raw")
        yield t, f


def _timeline(run_frames):
    """Seconds since the start of the run - from the sim's own clock if it ticks every frame,
    otherwise from the PC clock."""
    sim = [f["abs_time_s"] for _, f in run_frames]
    if all(b > a for a, b in zip(sim, sim[1:])):
        return [s - sim[0] for s in sim], "sim clock"
    pc = [t for t, _ in run_frames]
    return [p - pc[0] for p in pc], "pc clock"


def _fmt(x):
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:.8f}".rstrip("0").rstrip(".")
    return x


class TakeoffRecorder:
    """Watches the frame stream and cuts out individual takeoff runs."""

    def __init__(self, out_dir, columns=SUMMARY_COLUMNS):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.summary_path = self.out_dir / "runs_summary.csv"
        self.columns = columns                 # header for a new runs_summary.csv
        self.summary_columns = columns         # columns actually written (an older file keeps its own)
        if self.summary_path.exists() and self.summary_path.stat().st_size > 0:
            with open(self.summary_path, newline="", encoding="utf-8-sig") as fh:
                header = next(csv.reader(fh), [])
            # A summary started by an older version has fewer columns: keep appending in its layout
            if header and set(header) <= set(columns):
                self.summary_columns = header
                missing = [c for c in columns if c not in header]
                if missing:
                    print(f"Note: {self.summary_path} was started by an older version of this script, so it "
                          f"won't get the new columns ({', '.join(missing)}). Each run_NNN.csv still has them.")
        numbers = [int(p.stem[4:]) for p in self.out_dir.glob("run_*.csv") if p.stem[4:].isdigit()]
        self.run_number = max(numbers, default=0) + 1
        self.state = "WAITING"
        self.frames = []
        self.still = None          # last frame while armed and standing still
        self.air_index = None      # index of the first airborne frame (liftoff candidate)
        self.bounced = False

    def feed(self, t, f):
        stopped = f["on_ground"] > 0.5 and f["gs_kt"] < START_SPEED_KT
        if self.state == "WAITING":
            if stopped and f["throttle_pct"] >= ARM_THROTTLE_PCT:
                self.state, self.still = "ARMED", (t, f)
                print(f"\nRun {self.run_number:03d} ARMED | mass {f['weight_lb']:.0f} lb "
                      f"({f['weight_lb'] * LB_TO_KG:.1f} kg) | flaps {f['flaps_deg']:.1f} deg | "
                      f"trim {f['trim_pct']:+.0f}% | wind {f['wind_kt']:.1f} kt | {f['temp_c']:.1f} C | "
                      f"elevation {f['ground_alt_m']:.0f} m | mixture {f['mixture_pct']:.0f}%")
                if abs(f["flaps_deg"] - EXPECTED_FLAPS_DEG) > 1.0:
                    print(f"  CHECK: flaps are at {f['flaps_deg']:.1f} deg, not {EXPECTED_FLAPS_DEG:.0f} "
                          "(if they're still moving, wait before releasing the brakes)")
                if f["wind_kt"] > 0.5:
                    print("  CHECK: there is wind - set it to zero")
                print("  Release the brakes when ready.")
        elif self.state == "ARMED":
            if f["throttle_pct"] < ARM_THROTTLE_PCT or f["on_ground"] < 0.5:
                self.state = "WAITING"
                print("Disarmed (throttle reduced).")
            elif stopped:
                self.still = (t, f)
            else:
                self.state = "ROLLING"
                self.frames = [self.still, (t, f)]
                self.air_index, self.bounced = None, False
                print("Rolling...")
        elif self.state == "ROLLING":
            self.frames.append((t, f))
            if f["on_ground"] > 0.5:
                if self.air_index is not None:          # came back down: that was a bounce
                    self.air_index, self.bounced = None, True
                if f["throttle_pct"] < ABORT_THROTTLE_PCT:
                    self._cancel("throttle pulled back")
                elif t - self.frames[0][0] > MAX_ROLL_S:
                    self._cancel("no liftoff")
            elif self.air_index is None:
                self.air_index = len(self.frames) - 1   # first frame in the air
            elif t - self.frames[self.air_index][0] >= AIRBORNE_CONFIRM_S:
                self._save()

    def _cancel(self, reason):
        print(f"Run cancelled ({reason}) - nothing saved.")
        self.state, self.frames = "WAITING", []

    def _save(self):
        run, k = self.frames, self.air_index
        times, source = _timeline(run)
        f0 = run[0][1]
        fg, fa = run[k - 1][1], run[k][1]         # last frame on the ground, first in the air
        t_lo = (times[k - 1] + times[k]) / 2      # liftoff = halfway between those two frames
        lat_lo = (fg["lat_deg"] + fa["lat_deg"]) / 2
        lon_lo = (fg["lon_deg"] + fa["lon_deg"]) / 2
        roll_m = ground_distance_m(f0["lat_deg"], f0["lon_deg"], lat_lo, lon_lo)

        # Independent check: integrate ground speed over time (trapezium rule)
        v = [f["gs_kt"] * KT_TO_MS for _, f in run]
        check_m = sum((v[i] + v[i - 1]) / 2 * (times[i] - times[i - 1]) for i in range(1, k))
        check_m += (v[k - 1] + (v[k - 1] + v[k]) / 2) / 2 * (t_lo - times[k - 1])

        on_runway = [f for _, f in run[:k + 1]]
        temp_c = mean(f["temp_c"] for f in on_runway)
        p_pa = mean(f["pressure_inhg"] for f in on_runway) * INHG_TO_PA

        def at_liftoff(key):
            return (fg[key] + fa[key]) / 2

        name = f"run_{self.run_number:03d}.csv"
        with open(self.out_dir / name, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(FRAME_COLUMNS)
            for (_, f), ts in zip(run, times):
                dist = ground_distance_m(f0["lat_deg"], f0["lon_deg"], f["lat_deg"], f["lon_deg"])
                row = {**f, "t_s": ts, "dist_m": dist}
                writer.writerow([_fmt(row[c]) for c in FRAME_COLUMNS])

        s = {
            "run": self.run_number,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "mass_lb": round(f0["weight_lb"], 1),
            "mass_kg": round(f0["weight_lb"] * LB_TO_KG, 2),
            "ground_roll_m": round(roll_m, 2),
            "ground_roll_check_m": round(check_m, 2),
            "roll_time_s": round(t_lo - times[0], 3),
            "liftoff_ias_kt": round(at_liftoff("ias_kt"), 2),
            "liftoff_tas_kt": round(at_liftoff("tas_kt"), 2),
            "liftoff_gs_kt": round(at_liftoff("gs_kt"), 2),
            "liftoff_aoa_deg": round(at_liftoff("aoa_deg"), 2),
            "liftoff_pitch_deg": round(at_liftoff("pitch_deg"), 2),
            "flaps_deg": round(f0["flaps_deg"], 1),
            "trim_pct": round(f0["trim_pct"], 1),
            "max_rpm": round(max(f["rpm"] for f in on_runway)),
            "temp_c": round(temp_c, 2),
            "pressure_hpa": round(p_pa / 100, 2),
            "air_density_kg_m3": round(p_pa / (R_AIR * (temp_c + 273.15)), 4),
            "max_wind_kt": round(max(f["wind_kt"] for f in on_runway), 2),
            "bounced": self.bounced,
            "flaps_moved": max(f["flaps_deg"] for f in on_runway) - min(f["flaps_deg"] for f in on_runway) > 1.0,
            "time_source": source,
            "file": name,
            "elevation_m": round(f0["ground_alt_m"], 1),
            "mixture_pct": round(f0["mixture_pct"], 1),
        }
        s = self.finish_summary(s)
        new_file = not self.summary_path.exists() or self.summary_path.stat().st_size == 0
        with open(self.summary_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                self.summary_columns = self.columns
                writer.writerow(self.columns)
            writer.writerow([_fmt(s.get(c, "")) for c in self.summary_columns])

        print(f"Run {self.run_number:03d} saved: mass {s['mass_kg']:.1f} kg | ground roll "
              f"{s['ground_roll_m']:.1f} m (check {s['ground_roll_check_m']:.1f} m) | liftoff "
              f"{s['liftoff_ias_kt']:.1f} KIAS, pitch {s['liftoff_pitch_deg']:.1f} deg, AoA {s['liftoff_aoa_deg']:.1f} deg | "
              f"{s['roll_time_s']:.1f} s")
        if s["max_wind_kt"] > 0.5:
            print(f"  NOTE: wind reached {s['max_wind_kt']:.1f} kt - set calm wind.")
        if s["bounced"]:
            print("  NOTE: it bounced before lifting off for good - consider redoing this run.")
        if s["flaps_moved"]:
            print("  NOTE: the flaps moved during the roll.")
        self.after_save(s)
        self.run_number += 1
        self.state, self.frames = "WAITING", []

    # Hooks for scripts built on this one (ee_logger.py)
    def finish_summary(self, s):
        """Add or change columns in a run's summary row before it's written."""
        return s

    def after_save(self, s):
        """Called once a run and its summary row are saved."""


def check(feed, frame_queue):
    print("Live readout - Ctrl+C to stop. Move the throttle and controls and check the numbers react.")
    print("Wind check: sit still for 2 minutes - the wind 'max' should stay at 0.0.\n")
    last, count, max_wind = time.perf_counter(), 0, 0.0
    for t, f in frames(feed, frame_queue):
        count += 1
        max_wind = max(max_wind, f["wind_kt"])
        if t - last >= 1.0:
            rate = count / (t - last)
            last, count = t, 0
            print(f"{'GROUND' if f['on_ground'] > 0.5 else 'AIR   '} | mass {f['weight_lb']:6.0f} lb | "
                  f"GS {f['gs_kt']:5.1f} kt | IAS {f['ias_kt']:5.1f} kt | throttle {f['throttle_pct']:3.0f}% | "
                  f"{f['rpm']:4.0f} rpm | pitch {f['pitch_deg']:5.1f} deg | AoA {f['aoa_deg']:5.1f} deg | "
                  f"flaps {f['flaps_deg']:.1f} deg | trim {f['trim_pct']:+.0f}% | {f['temp_c']:.1f} C {f['pressure_inhg'] * INHG_TO_PA / 100:.1f} hPa | "
                  f"elevation {f['ground_alt_m']:.0f} m | mixture {f['mixture_pct']:.0f}% | "
                  f"wind {f['wind_kt']:.1f} kt (max {max_wind:.1f}) | {rate:.0f} samples/s")


def main(recorder_class=TakeoffRecorder, folder="IA",
         description="Record IA takeoff runs (mass vs ground roll) through SimConnect."):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--check", action="store_true", help="only show live values (connection test)")
    parser.add_argument("--dll", help="path to another SimConnect.dll, e.g. the one in the MSFS 2024 SDK")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / folder),
                        help=f"folder for the CSV files (default: {folder} next to this script)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    frame_queue = queue.Queue()
    try:
        feed = FrameFeed(frame_queue, args.dll)
    except ConnectionError:
        sys.exit("Couldn't find Flight Simulator - start MSFS and load into a flight first.")
    except OSError as err:
        sys.exit(f"Couldn't load SimConnect.dll ({err}).\n"
                 "Make sure you're using 64-bit Python, or try --dll with the SimConnect.dll from the MSFS 2024 SDK.")
    if not feed.ok:
        sys.exit("Couldn't connect to Flight Simulator. Is it running and fully loaded?")

    print("Connected to Flight Simulator.")
    try:
        feed.start_stream()
        if args.check:
            check(feed, frame_queue)
        else:
            recorder = recorder_class(args.out)
            print(f"Saving runs to: {recorder.out_dir}")
            print("Line up, hold the brakes, full throttle -> release the brakes -> take off.  Ctrl+C to quit.")
            for t, f in frames(feed, frame_queue):
                recorder.feed(t, f)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        feed.close()


if __name__ == "__main__":
    main()
