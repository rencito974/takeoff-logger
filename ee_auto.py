r"""
ee_auto.py - automatic EE runs: puts the plane back at the start, accelerates to 60 KIAS, records the roll
=========================================================================================================
Same EE question and EE folder as ee_logger.py, but the sim is driven through SimConnect, so every run
starts the same way and there's no rotation technique in it. The plane never lifts off: each run ends at
END_IAS_KT, and the summary row gets the distance to MEASURE_IAS_KT (55 kt) instead of a ground roll.

    1. At start-up: four seats at SEAT_WEIGHT_LB and both fuel tanks full (FULL_TANK_GAL each), then the
       mass must read TARGET_MASS_LB. The spot where the plane is standing becomes the start point.
    2. Before each run: back to the start point, parking brake on, flaps 10, trim TRIM_PCT, full throttle,
       RUN_UP_S for the RPM to settle. Then it checks mass, flaps and wind, and stops if anything is off.
    3. Releases the parking brake and records every frame exactly like the normal logger.
    4. At END_IAS_KT: throttle idle, parking brake on, run saved.
    After the last run the plane goes back to the start point, ready for the next session.

The weather is NOT set here: MSFS 2024 doesn't let SimConnect change it (its SimConnect weather functions
are deprecated and the AMBIENT variables can't be written). Pick the temperature and zero wind in the
sim's weather panel first, as for manual runs.

USE (load into the flight, stop at the start of the runway with the engine running, THEN run):
    py ee_auto.py                3 automatic runs  (Ctrl+C to stop)
    py ee_auto.py --runs 5       5 runs
    py ee_auto.py --fill         only fill in the new runs_summary.csv columns (no sim needed)

OUTPUT (folder "EE", shared with ee_logger.py, so the run numbering carries on across both)
    run_NNN.csv          every simulator frame, same columns as manual runs
    runs_summary.csv     one row per run. Auto runs leave the liftoff columns (and bounced) empty.
                         Four columns are added at the end:
        dist_to_55kias_m         brake release to where IAS passes 55 kt, interpolated between the frames either side
        check_dist_to_55kias_m   the same distance from integrating ground speed
        tas_at_55kias_kt         true airspeed at that point
        mode                     auto or manual
        For manual runs they're filled in from run_NNN.csv every time this script starts (or with --fill).
    temperature_series.csv, altitude_series.csv    rebuilt the same way as ee_logger.py does it
"""

import argparse
import csv
import logging
import os
import queue
import struct
import sys
import time
from ctypes import POINTER, addressof, c_double, cast, sizeof, string_at
from ctypes.wintypes import DWORD
from datetime import datetime
from pathlib import Path
from statistics import mean

import ee_logger as ee
import takeoff_logger as tl
from SimConnect.Constants import SIMCONNECT_GROUP_PRIORITY_HIGHEST, SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_UNUSED
from SimConnect.Enum import (
    SIMCONNECT_DATA_INITPOSITION,
    SIMCONNECT_DATA_REQUEST_FLAG,
    SIMCONNECT_DATATYPE,
    SIMCONNECT_PERIOD,
    SIMCONNECT_RECV_ID,
    SIMCONNECT_RECV_SIMOBJECT_DATA,
)

# ------------------------------------------------------------------ settings
RUNS = 3                    # runs in a row, unless --runs says otherwise
TARGET_MASS_LB = 2539.0     # full fuel + four 159 lb people
MASS_TOLERANCE_LB = 2.0
SEAT_WEIGHT_LB = 159.0      # each of the four seats
FULL_TANK_GAL = 25.7        # what the sim's own "full fuel" puts in each wing tank (capacity 28 gal incl. unusable)
TRIM_PCT = 13.6             # elevator trim of the manual runs
TRIM_TOLERANCE_PCT = 0.1
FLAPS_HANDLE_INDEX = 1      # handle position for 10 degrees
RUN_UP_S = 3.0              # full throttle on the parking brake before each run, for the RPM to settle
MAX_WIND_KT = 0.05          # anything that shows as 0.0 kt
MEASURE_IAS_KT = 55.0       # the summary records distance and TAS where IAS passes this
END_IAS_KT = 60.0           # throttle idle and brakes on here: the run is over
START_TOLERANCE_M = 3.0     # how close to the start point the plane must be put back

AUTO_COLUMNS = ["dist_to_55kias_m", "check_dist_to_55kias_m", "tas_at_55kias_kt", "mode"]
SUMMARY_COLUMNS = ee.EE_COLUMNS + AUTO_COLUMNS

SEAT_WORDS = ("pilot", "passenger", "seat")    # payload stations whose name has one of these are seats
EVENT_FLAG_GROUPID_IS_PRIORITY = 16
FIRST_ID = 901              # our SimConnect IDs start after the frame stream's 900


class Stop(Exception):
    """Something is off: stop the runs and say what."""


class SimLink(tl.FrameFeed):
    """FrameFeed that can also read variables once, write variables, send events and move the plane."""

    def __init__(self, frame_queue, dll_path=None):
        self.replies = {}            # request id -> raw reply bytes, None while waiting
        self.read_defs = {}          # tuple of variables -> definition id
        self.write_defs = {}         # (variable, units) -> definition id
        self.events = {}             # event name -> client event id
        self.last_id = FIRST_ID
        self.position_def = None
        super().__init__(frame_queue, dll_path)

    def _new_id(self):
        self.last_id += 1
        return self.last_id

    def my_dispatch_proc(self, pData, cbData, pContext):
        # Replies to read() are kept here; everything else goes to the frame stream as usual.
        try:
            if pData.contents.dwID == SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                msg = cast(pData, POINTER(SIMCONNECT_RECV_SIMOBJECT_DATA)).contents
                if msg.dwRequestID in self.replies:
                    start = SIMCONNECT_RECV_SIMOBJECT_DATA.dwData.offset
                    self.replies[msg.dwRequestID] = string_at(addressof(msg) + start, cbData - start)
                    return
        except Exception as err:
            print(f"\nError while reading SimConnect data: {err}")
            return
        super().my_dispatch_proc(pData, cbData, pContext)

    def read(self, variables, timeout=3.0):
        """Values of (variable, units) pairs, read once. Units "string" reads a text variable."""
        key = tuple(variables)
        if key not in self.read_defs:
            self.read_defs[key] = def_id = self._new_id()
            for name, units in variables:
                text = units == "string"
                self.dll.AddToDataDefinition(
                    self.hSimConnect, def_id, name.encode(), None if text else units.encode(),
                    SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_STRING256 if text
                    else SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64, 0, SIMCONNECT_UNUSED)
                self._remember_packet(f"the variable '{name}'")
        request = self._new_id()
        self.replies[request] = None
        self.dll.RequestDataOnSimObject(
            self.hSimConnect, request, self.read_defs[key], SIMCONNECT_OBJECT_ID_USER,
            SIMCONNECT_PERIOD.SIMCONNECT_PERIOD_ONCE,
            SIMCONNECT_DATA_REQUEST_FLAG.SIMCONNECT_DATA_REQUEST_FLAG_DEFAULT, 0, 0, 0)
        self._remember_packet("a one-off data request")
        end = time.perf_counter() + timeout
        while self.replies[request] is None:
            if time.perf_counter() > end:
                del self.replies[request]
                raise Stop(f"The sim didn't answer when asked for {', '.join(n for n, _ in variables)}.")
            time.sleep(0.01)
        raw, values, at = self.replies.pop(request), [], 0
        for _, units in variables:
            if units == "string":
                values.append(raw[at:at + 256].split(b"\0")[0].decode("utf-8", "replace"))
                at += 256
            else:
                values.append(struct.unpack_from("<d", raw, at)[0])
                at += 8
        return values

    def write(self, name, units, value):
        """Set one simulator variable."""
        if (name, units) not in self.write_defs:
            self.write_defs[name, units] = def_id = self._new_id()
            self.dll.AddToDataDefinition(self.hSimConnect, def_id, name.encode(), units.encode(),
                                         SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_FLOAT64, 0, SIMCONNECT_UNUSED)
            self._remember_packet(f"the variable '{name}'")
        data = c_double(value)
        self.dll.SetDataOnSimObject(self.hSimConnect, self.write_defs[name, units], SIMCONNECT_OBJECT_ID_USER,
                                    0, 0, sizeof(data), addressof(data))
        self._remember_packet(f"setting '{name}'")

    def send(self, event, value=0):
        """Send a key event, e.g. send("FLAPS_1") or send("PARKING_BRAKE_SET", 1)."""
        if event not in self.events:
            self.events[event] = self._new_id()
            self.dll.MapClientEventToSimEvent(self.hSimConnect, self.events[event], event.encode())
            self._remember_packet(f"the event '{event}'")
        self.dll.TransmitClientEvent(self.hSimConnect, SIMCONNECT_OBJECT_ID_USER, self.events[event],
                                     DWORD(value & 0xFFFFFFFF), SIMCONNECT_GROUP_PRIORITY_HIGHEST,
                                     EVENT_FLAG_GROUPID_IS_PRIORITY)
        self._remember_packet(f"the event '{event}'")

    def place(self, start):
        """Put the plane at the start point, on the ground and stopped."""
        if self.position_def is None:
            self.position_def = self._new_id()
            self.dll.AddToDataDefinition(self.hSimConnect, self.position_def, b"Initial Position", None,
                                         SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_INITPOSITION, 0, SIMCONNECT_UNUSED)
            self._remember_packet("the start position")
        init = SIMCONNECT_DATA_INITPOSITION(start["lat"], start["lon"], start["alt_ft"], start["pitch"],
                                            start["bank"], start["heading"], 1, 0)
        self.dll.SetDataOnSimObject(self.hSimConnect, self.position_def, SIMCONNECT_OBJECT_ID_USER,
                                    0, 0, sizeof(init), addressof(init))
        self._remember_packet("moving the plane to the start point")


class Stream:
    """The per-frame data: read in order while recording, or skip to the newest frame."""

    def __init__(self, link, frame_queue):
        self.queue = frame_queue
        self.frames = tl.frames(link, frame_queue)

    def next(self):
        frame = next(self.frames, None)
        if frame is None:
            raise Stop("Flight Simulator stopped sending data.")
        return frame

    def latest(self):
        while self.queue.qsize() > 1:      # frames that piled up while we were busy
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        return self.next()

    def watch(self, seconds):
        """All frames from now for this many seconds."""
        frames = [self.latest()]
        while frames[-1][0] - frames[0][0] < seconds:
            frames.append(self.next())
        return frames

    def wait_for(self, condition, seconds, what):
        t0, f = self.latest()
        t = t0
        while not condition(f):
            if t - t0 > seconds:
                raise Stop(f"Gave up after {seconds:.0f} s waiting for {what}.")
            t, f = self.next()
        return f


# ------------------------------------------------------------------ the 55 kt columns
def speed_point(frames, ias_kt=MEASURE_IAS_KT):
    """(distance, check distance, TAS) where IAS first passes ias_kt, from a run's frames (dicts with
    t_s, lat_deg, lon_deg, gs_kt, ias_kt, tas_kt). None if the run never got that fast."""
    j = next((i for i in range(1, len(frames)) if frames[i]["ias_kt"] >= ias_kt), None)
    if j is None:
        return None
    a, b = frames[j - 1], frames[j]
    x = (ias_kt - a["ias_kt"]) / (b["ias_kt"] - a["ias_kt"])    # how far between the two frames

    def at(key):
        return a[key] + x * (b[key] - a[key])

    f0 = frames[0]
    dist = tl.ground_distance_m(f0["lat_deg"], f0["lon_deg"], at("lat_deg"), at("lon_deg"))
    # Independent check: integrate ground speed over time (trapezium rule), like ground_roll_check_m
    v = [f["gs_kt"] * tl.KT_TO_MS for f in frames]
    t = [f["t_s"] for f in frames]
    check = sum((v[i] + v[i - 1]) / 2 * (t[i] - t[i - 1]) for i in range(1, j))
    check += (v[j - 1] + at("gs_kt") * tl.KT_TO_MS) / 2 * (at("t_s") - t[j - 1])
    return round(dist, 2), round(check, 2), round(at("tas_kt"), 2)


def speed_point_from_file(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        frames = [{k: float(r[k]) for k in ("t_s", "lat_deg", "lon_deg", "gs_kt", "ias_kt", "tas_kt")}
                  for r in csv.DictReader(fh)]
    return speed_point(frames)


def fill_new_columns(out_dir):
    """Add AUTO_COLUMNS to runs_summary.csv and fill them in for runs that don't have them yet (manual
    runs, from their run_NNN.csv). Values already there are never changed. Returns the runs filled in."""
    path = out_dir / "runs_summary.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    header = rows[0] + [c for c in AUTO_COLUMNS if c not in rows[0]]
    col = {c: i for i, c in enumerate(header)}
    changed, filled = header != rows[0], []
    for row in rows[1:]:
        if not row:
            continue
        row += [""] * (len(header) - len(row))     # rows written by ee_logger.py stop before these columns
        if not row[col["mode"]]:
            row[col["mode"]] = "manual"             # only manual runs come without a mode
            changed = True
        frame_file = out_dir / row[col["file"]]
        if not row[col["dist_to_55kias_m"]] and row[col["file"]] and frame_file.exists():
            point = speed_point_from_file(frame_file)
            if point:
                for c, value in zip(AUTO_COLUMNS, point):
                    row[col[c]] = tl._fmt(value)
                filled.append(row[col["run"]])
                changed = True
    if changed:
        rows[0] = header
        temp = path.with_name(path.name + ".tmp")
        with open(temp, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        try:
            os.replace(temp, path)
        except PermissionError:
            temp.unlink()
            raise Stop("Couldn't update runs_summary.csv - is it open in Excel? Close it and start again.")
        ee.write_series_files(out_dir)
    return filled


# ------------------------------------------------------------------ driving the sim
def fill_tank(link, side):
    """Set a wing tank to FULL_TANK_GAL as the sim reads it back. A write to the QUANTITY variable
    includes the unusable fuel but a read leaves it out (1.5 gal in the C172), so correct from the read."""
    name = f"FUEL TANK {side} MAIN QUANTITY"
    written = None
    for _ in range(4):
        now = link.read([(name, "Gallons")])[0]
        if abs(now - FULL_TANK_GAL) <= 0.01:
            return
        written = FULL_TANK_GAL if written is None else written + (FULL_TANK_GAL - now)
        link.write(name, "Gallons", written)
        time.sleep(0.3)
    raise Stop(f"Couldn't fill the {side.lower()} tank to {FULL_TANK_GAL} gal (it reads {now:.2f} gal).")


def load_plane(link, stream):
    """Four seats at SEAT_WEIGHT_LB, both tanks full, and check the total mass."""
    count = round(link.read([("PAYLOAD STATION COUNT", "Number")])[0])
    names = link.read([(f"PAYLOAD STATION NAME:{i}", "string") for i in range(1, count + 1)])
    seats = [i for i, name in enumerate(names, 1) if any(w in name.lower() for w in SEAT_WORDS)]
    if len(seats) != 4:
        raise Stop(f"Expected 4 seats among the payload stations, found {len(seats)}: {names}")
    for i in seats:
        link.write(f"PAYLOAD STATION WEIGHT:{i}", "Pounds", SEAT_WEIGHT_LB)
    fill_tank(link, "LEFT")
    fill_tank(link, "RIGHT")
    try:
        f = stream.wait_for(lambda f: abs(f["weight_lb"] - TARGET_MASS_LB) <= MASS_TOLERANCE_LB, 5, "the mass")
    except Stop:
        weights = link.read([(f"PAYLOAD STATION WEIGHT:{i}", "Pounds") for i in range(1, count + 1)])
        fuel = link.read([("FUEL TANK LEFT MAIN QUANTITY", "Gallons"), ("FUEL TANK RIGHT MAIN QUANTITY", "Gallons"),
                          ("FUEL TOTAL QUANTITY WEIGHT", "Pounds"), ("TOTAL WEIGHT", "Pounds")])
        stations = ", ".join(f"{n} {w:.0f} lb" for n, w in zip(names, weights))
        raise Stop(f"The mass is {fuel[3]:.1f} lb, not {TARGET_MASS_LB:.0f} +/- {MASS_TOLERANCE_LB:.0f} lb. "
                   f"Payload: {stations}. Fuel: {fuel[0]:.1f} + {fuel[1]:.1f} gal ({fuel[2]:.0f} lb).")
    print(f"Loaded: four seats at {SEAT_WEIGHT_LB:.0f} lb, both tanks full ({FULL_TANK_GAL} gal each) "
          f"-> mass {f['weight_lb']:.1f} lb")


def read_start(link, stream):
    f = stream.latest()[1]
    if f["on_ground"] < 0.5 or f["gs_kt"] >= tl.START_SPEED_KT:
        raise Stop("Stop the plane at the start of the runway first.")
    if f["rpm"] < 400:
        raise Stop("Start the engine first.")
    names = ["lat", "lon", "alt_ft", "pitch", "bank", "heading"]
    values = link.read([("PLANE LATITUDE", "Degrees"), ("PLANE LONGITUDE", "Degrees"), ("PLANE ALTITUDE", "Feet"),
                        ("PLANE PITCH DEGREES", "Degrees"), ("PLANE BANK DEGREES", "Degrees"),
                        ("PLANE HEADING DEGREES TRUE", "Degrees")])
    start = dict(zip(names, values))
    print(f"Start point: {start['lat']:.6f}, {start['lon']:.6f} | heading {start['heading']:.1f} deg true | "
          f"elevation {f['ground_alt_m']:.0f} m")
    return start


def from_start(start, f):
    return tl.ground_distance_m(start["lat"], start["lon"], f["lat_deg"], f["lon_deg"])


def set_trim(link, stream):
    """Bring the elevator trim to TRIM_PCT, correcting from what the sim reads back."""
    f = stream.latest()[1]
    for _ in range(6):
        error = TRIM_PCT - f["trim_pct"]
        if abs(error) <= TRIM_TOLERANCE_PCT:
            return
        value = round((f["trim_pct"] + error) / 100 * 16383)
        link.send("AXIS_ELEV_TRIM_SET", max(-16383, min(16383, value)))
        time.sleep(0.5)
        f = stream.latest()[1]
    raise Stop(f"Couldn't set the elevator trim to {TRIM_PCT}% (it reads {f['trim_pct']:.1f}%).")


def back_to_start(link, stream, start):
    """Throttle idle, parking brake on, and the plane put back at the start point."""
    link.send("THROTTLE_CUT")
    link.send("PARKING_BRAKE_SET", 1)
    link.place(start)
    stream.wait_for(lambda f: f["on_ground"] > 0.5 and f["gs_kt"] < tl.START_SPEED_KT
                    and from_start(start, f) <= START_TOLERANCE_M, 10, "the plane to settle at the start point")


def prepare(link, stream, start, number):
    """Back to the start point, configured and at full throttle on the parking brake."""
    back_to_start(link, stream, start)
    link.send("FLAPS_1")
    stream.wait_for(lambda f: round(f["flaps_index"]) == FLAPS_HANDLE_INDEX
                    and abs(f["flaps_deg"] - tl.EXPECTED_FLAPS_DEG) <= 1.0, 20, "the flaps to reach 10 deg")
    set_trim(link, stream)
    link.send("PARKING_BRAKE_SET", 1)
    if link.read([("BRAKE PARKING POSITION", "Position")])[0] < 0.5:
        raise Stop("The parking brake didn't come on.")
    link.send("THROTTLE_FULL")
    run_up = stream.watch(RUN_UP_S)
    f = run_up[-1][1]
    print(f"\nRun {number:03d} ARMED (auto) | mass {f['weight_lb']:.0f} lb ({f['weight_lb'] * tl.LB_TO_KG:.1f} kg) | "
          f"flaps {f['flaps_deg']:.1f} deg | trim {f['trim_pct']:+.1f}% | wind {f['wind_kt']:.1f} kt | "
          f"{f['temp_c']:.1f} C | elevation {f['ground_alt_m']:.0f} m | mixture {f['mixture_pct']:.0f}% | "
          f"{f['rpm']:.0f} rpm")
    problems = []
    if abs(f["weight_lb"] - TARGET_MASS_LB) > MASS_TOLERANCE_LB:
        problems.append(f"mass is {f['weight_lb']:.1f} lb, not {TARGET_MASS_LB:.0f} +/- {MASS_TOLERANCE_LB:.0f}")
    if round(f["flaps_index"]) != FLAPS_HANDLE_INDEX or abs(f["flaps_deg"] - tl.EXPECTED_FLAPS_DEG) > 1.0:
        problems.append(f"flaps are at {f['flaps_deg']:.1f} deg (handle {f['flaps_index']:.0f}), not 10 deg")
    if f["wind_kt"] >= MAX_WIND_KT:
        problems.append(f"wind is {f['wind_kt']:.2f} kt, not 0.0 - set calm wind in the weather panel")
    if abs(f["trim_pct"] - TRIM_PCT) > TRIM_TOLERANCE_PCT:
        problems.append(f"trim is {f['trim_pct']:.1f}%, not {TRIM_PCT}%")
    if f["throttle_pct"] < 99:
        problems.append(f"throttle is at {f['throttle_pct']:.0f}%, not full (is a throttle axis overriding it?)")
    if any(g["gs_kt"] >= tl.START_SPEED_KT for _, g in run_up):
        problems.append("the plane moved during the run-up: the parking brake isn't holding")
    if problems:
        raise Stop("Not running this one:\n  - " + "\n  - ".join(problems))


def drive(link, stream):
    """Release the parking brake and record from brake release to END_IAS_KT, cut like the normal logger:
    the run starts with the last frame standing still and ends with the first frame at END_IAS_KT."""
    link.send("PARKING_BRAKE_SET", 0)
    released = time.perf_counter()
    still, run = None, []
    while True:
        t, f = stream.next()
        if not run:
            if f["on_ground"] > 0.5 and f["gs_kt"] < tl.START_SPEED_KT:
                still = (t, f)
                if time.perf_counter() - released > 10:
                    raise Stop("The plane didn't start rolling after the parking brake was released.")
                continue
            if still is None:
                raise Stop("The plane was already moving when the parking brake was released.")
            run = [still, (t, f)]
            print("Rolling...")
        else:
            run.append((t, f))
        if f["throttle_pct"] < tl.ABORT_THROTTLE_PCT:
            raise Stop("The throttle came back during the run - nothing saved.")
        if t - run[0][0] > tl.MAX_ROLL_S:
            raise Stop(f"Still below {END_IAS_KT:.0f} KIAS after {tl.MAX_ROLL_S:.0f} s - nothing saved.")
        if f["ias_kt"] >= END_IAS_KT:
            link.send("THROTTLE_CUT")
            link.send("PARKING_BRAKE_SET", 1)
            return run


def save(out_dir, number, run):
    """Write run_NNN.csv and its runs_summary.csv row; returns the row."""
    times, source = tl._timeline(run)
    f0 = run[0][1]
    name = f"run_{number:03d}.csv"
    with open(out_dir / name, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(tl.FRAME_COLUMNS)
        for (_, f), ts in zip(run, times):
            dist = tl.ground_distance_m(f0["lat_deg"], f0["lon_deg"], f["lat_deg"], f["lon_deg"])
            row = {**f, "t_s": ts, "dist_m": dist}
            writer.writerow([tl._fmt(row[c]) for c in tl.FRAME_COLUMNS])

    frames = [f for _, f in run]
    temp_c = mean(f["temp_c"] for f in frames)
    p_pa = mean(f["pressure_inhg"] for f in frames) * tl.INHG_TO_PA
    s = {
        "run": number,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "mass_lb": round(f0["weight_lb"], 1),
        "mass_kg": round(f0["weight_lb"] * tl.LB_TO_KG, 2),
        "flaps_deg": round(f0["flaps_deg"], 1),
        "trim_pct": round(f0["trim_pct"], 1),
        "max_rpm": round(max(f["rpm"] for f in frames)),
        "temp_c": round(temp_c, 2),
        "pressure_hpa": round(p_pa / 100, 2),
        "air_density_kg_m3": round(p_pa / (tl.R_AIR * (temp_c + 273.15)), 4),
        "max_wind_kt": round(max(f["wind_kt"] for f in frames), 2),
        "flaps_moved": max(f["flaps_deg"] for f in frames) - min(f["flaps_deg"] for f in frames) > 1.0,
        "time_source": source,
        "file": name,
        "elevation_m": round(f0["ground_alt_m"], 1),
        "mixture_pct": round(f0["mixture_pct"], 1),
        "mode": "auto",
    }
    s["series"], s["series_note"] = ee.classify(s["elevation_m"], s["temp_c"])
    point = speed_point_from_file(out_dir / name)   # from the saved file, exactly as for manual runs
    if point:
        s.update(zip(AUTO_COLUMNS, point))

    summary = out_dir / "runs_summary.csv"
    if summary.exists() and summary.stat().st_size > 0:
        with open(summary, newline="", encoding="utf-8-sig") as fh:
            columns = next(csv.reader(fh))
        new_file = False
    else:
        columns, new_file = SUMMARY_COLUMNS, True
    with open(summary, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(columns)
        writer.writerow([tl._fmt(s.get(c, "")) for c in columns])
    ee.write_series_files(out_dir)

    if point:
        print(f"Run {number:03d} saved (auto): {MEASURE_IAS_KT:.0f} KIAS after {point[0]:.1f} m "
              f"(check {point[1]:.1f} m), TAS {point[2]:.1f} kt | {END_IAS_KT:.0f} KIAS after {times[-1]:.1f} s | "
              f"{s['temp_c']:.1f} C, {s['pressure_hpa']:.1f} hPa, {s['air_density_kg_m3']:.4f} kg/m3")
    else:
        print(f"Run {number:03d} saved (auto), but IAS never passed {MEASURE_IAS_KT:.0f} kt?")
    if any(f["on_ground"] < 0.5 for f in frames):
        print("  CHECK: the wheels left the ground during this run.")
    if s["max_wind_kt"] > 0.5:
        print(f"  NOTE: wind reached {s['max_wind_kt']:.1f} kt - set calm wind.")
    if s["flaps_moved"]:
        print("  NOTE: the flaps moved during the roll.")
    if s["series"] == "neither":
        print(f"  CHECK: this run is in neither series ({s['series_note']}). Fix the temperature and redo it.")
    else:
        print(f"  EE series: {s['series']} ({s['elevation_m']:.0f} m, {s['temp_c']:.1f} C)")
    return s


def next_run_number(out_dir):
    numbers = [int(p.stem[4:]) for p in out_dir.glob("run_*.csv") if p.stem[4:].isdigit()]
    return max(numbers, default=0) + 1


def auto_runs(link, stream, out_dir, runs):
    load_plane(link, stream)
    start = read_start(link, stream)
    for i in range(runs):
        number = next_run_number(out_dir)
        prepare(link, stream, start, number)
        run = drive(link, stream)
        save(out_dir, number, run)
        stream.wait_for(lambda f: f["gs_kt"] < tl.START_SPEED_KT, 60, "the plane to stop")
    back_to_start(link, stream, start)
    print(f"\nDone: {runs} auto run{'s' if runs > 1 else ''}. Back at the start point, throttle idle, parking brake on.")


def main():
    parser = argparse.ArgumentParser(description="Automatic EE runs to 60 KIAS through SimConnect.")
    parser.add_argument("--runs", type=int, default=RUNS, help=f"runs in a row (default {RUNS})")
    parser.add_argument("--fill", action="store_true",
                        help="only fill in the new runs_summary.csv columns for runs that lack them")
    parser.add_argument("--dll", help="path to another SimConnect.dll, e.g. the one in the MSFS 2024 SDK")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "EE"),
                        help="folder for the CSV files (default: EE next to this script)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        filled = fill_new_columns(out_dir)
    except Stop as err:
        sys.exit(str(err))
    if filled:
        print(f"Filled in the {MEASURE_IAS_KT:.0f} kt columns for run{'s' if len(filled) > 1 else ''} "
              f"{', '.join(filled)} in {out_dir / 'runs_summary.csv'}")
    if args.fill:
        if not filled:
            print("Nothing to fill in.")
        return
    if args.runs < 1:
        sys.exit("--runs must be at least 1")

    frame_queue = queue.Queue()
    try:
        link = SimLink(frame_queue, args.dll)
    except ConnectionError:
        sys.exit("Couldn't find Flight Simulator - start MSFS and load into a flight first.")
    except OSError as err:
        sys.exit(f"Couldn't load SimConnect.dll ({err}).\n"
                 "Make sure you're using 64-bit Python, or try --dll with the SimConnect.dll from the MSFS 2024 SDK.")
    if not link.ok:
        sys.exit("Couldn't connect to Flight Simulator. Is it running and fully loaded?")

    print("Connected to Flight Simulator.")
    print(f"Saving runs to: {out_dir}")
    print("The weather isn't set by this script: pick the temperature and calm wind in the sim first.")
    try:
        link.start_stream()
        auto_runs(link, Stream(link, frame_queue), out_dir, args.runs)
    except (Stop, KeyboardInterrupt) as err:
        print(f"\nSTOPPED: {err}" if isinstance(err, Stop) else "\nStopped.")
        if not link.quit:
            link.send("THROTTLE_CUT")
            link.send("PARKING_BRAKE_SET", 1)
            print("Throttle idle, parking brake on.")
        sys.exit(1 if isinstance(err, Stop) else 0)
    finally:
        link.close()


if __name__ == "__main__":
    main()
