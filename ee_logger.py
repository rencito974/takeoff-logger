r"""
ee_logger.py - records Extended Essay takeoff runs and sorts each one into its series
=====================================================================================
EE question: how does air density, changed through temperature and altitude, affect the takeoff?
Runs are detected and recorded exactly like takeoff_logger.py (same CSV columns), but they go
into the EE folder and every run is labelled with the series it belongs to:

    temperature   at the baseline airport (below BASELINE_MAX_ELEVATION_M, e.g. Ezeiza), any temperature
    altitude      at a higher airport, at ALTITUDE_SERIES_TEMP_C
    both          at the baseline airport AND at ALTITUDE_SERIES_TEMP_C: the sea-level point of both series
    neither       at a higher airport but NOT at ALTITUDE_SERIES_TEMP_C - fix the temperature and redo it

USE (load into a flight and sit in the cockpit FIRST, then run):
    py ee_logger.py              record EE runs  (Ctrl+C to quit)
    py ee_logger.py --check      live readout, to test the connection

OUTPUT (folder "EE" next to this script)
    run_001.csv, run_002.csv ...   every simulator frame of each run
    runs_summary.csv               one row per run, with its series
    temperature_series.csv         just the temperature-series runs, coldest first
    altitude_series.csv            just the altitude-series runs, lowest first
    The two series files are rebuilt from runs_summary.csv after every run and at start-up, so a
    run whose row you delete from runs_summary.csv drops out of them the next time you start this.
"""

import csv

import takeoff_logger as tl

# ------------------------------------------------------------------ settings
BASELINE_MAX_ELEVATION_M = 100.0   # runs below this elevation are at the baseline airport (Ezeiza is about 20 m)
ALTITUDE_SERIES_TEMP_C = 15.0      # the temperature you hold fixed for the altitude series
TEMP_TOLERANCE_C = 1.0             # how far a run's temperature can be from that and still count

EE_COLUMNS = ["run", "series"] + [c for c in tl.SUMMARY_COLUMNS if c != "run"] + ["series_note"]

SERIES_FILES = {                   # file: (series labels it takes, column it's sorted by)
    "temperature_series.csv": ({"temperature", "both"}, "temp_c"),
    "altitude_series.csv": ({"altitude", "both"}, "elevation_m"),
}


def classify(elevation_m, temp_c):
    """The series a run belongs to, plus a note saying why when it fits neither."""
    at_series_temp = abs(temp_c - ALTITUDE_SERIES_TEMP_C) <= TEMP_TOLERANCE_C
    if elevation_m < BASELINE_MAX_ELEVATION_M:
        return ("both" if at_series_temp else "temperature"), ""
    if at_series_temp:
        return "altitude", ""
    return "neither", (f"{temp_c:.1f} C at {elevation_m:.0f} m, but the altitude series is at "
                       f"{ALTITUDE_SERIES_TEMP_C:.0f} C")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("inf")


def write_series_files(out_dir):
    """Rebuild temperature_series.csv and altitude_series.csv from runs_summary.csv."""
    summary = out_dir / "runs_summary.csv"
    if not summary.exists():
        return
    with open(summary, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        columns = reader.fieldnames or EE_COLUMNS
    for name, (labels, sort_column) in SERIES_FILES.items():
        picked = sorted((r for r in rows if r.get("series") in labels),
                        key=lambda r: (_number(r.get(sort_column)), _number(r.get("run"))))
        try:
            with open(out_dir / name, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns)
                writer.writeheader()
                writer.writerows(picked)
        except PermissionError:
            print(f"  Couldn't update {name} - is it open in Excel? Close it: it's rebuilt after the next run.")


class EERecorder(tl.TakeoffRecorder):
    """TakeoffRecorder that labels each run with its EE series and keeps the series files up to date."""

    def __init__(self, out_dir):
        super().__init__(out_dir, EE_COLUMNS)
        print(f"Sorting runs: below {BASELINE_MAX_ELEVATION_M:.0f} m = temperature series, "
              f"altitude series at {ALTITUDE_SERIES_TEMP_C:.0f} C (+/- {TEMP_TOLERANCE_C:.0f} C)")
        write_series_files(self.out_dir)

    def finish_summary(self, s):
        s["series"], s["series_note"] = classify(s["elevation_m"], s["temp_c"])
        return s

    def after_save(self, s):
        if s["series"] == "neither":
            print(f"  CHECK: this run is in neither series ({s['series_note']}). Fix the temperature and redo it.")
        else:
            print(f"  EE series: {s['series']} ({s['elevation_m']:.0f} m, {s['temp_c']:.1f} C)")
        write_series_files(self.out_dir)


if __name__ == "__main__":
    tl.main(EERecorder, folder="EE", description="Record EE takeoff runs and sort them into series.")
