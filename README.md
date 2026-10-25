# Takeoff Logger

Records takeoff runs in Microsoft Flight Simulator (2020 and 2024) through SimConnect. Every run is saved frame by frame, plus one summary row with the ground roll, liftoff speeds and air conditions.

Two investigations use it, and each one has its own folder:

- **IA** in `IA/`: how aircraft mass affects the takeoff ground roll
- **EE** in `EE/`: how air density, changed through temperature and altitude, affects the takeoff

## Setup

Windows with 64-bit Python 3, then:

```
py -m pip install SimConnect
```

## Recording runs

Load into a flight and sit in the cockpit first, then double-click one of these. The window's title says where the runs are going.

- **Check sim connection.bat**: a live readout to test the connection. Sit still for 2 minutes: the wind max should stay at 0.0.
- **Record IA runs.bat**: records IA runs into `IA/`
- **Record EE runs.bat**: records EE runs into `EE/` and sorts each one into its series
- **Record EE auto runs.bat**: flies 3 EE runs by itself to 60 KIAS and records them into `EE/` (see [EE auto runs](#ee-auto-runs))

Stop with Ctrl+C. Windows may then ask "Terminate batch job (Y/N)?". Either answer is fine, because finished runs are already saved.

The shortcuts run these commands, which also work typed into a terminal in this folder:

```
py takeoff_logger.py --check
```

```
py takeoff_logger.py
```

```
py ee_logger.py
```

```
py ee_auto.py
```

Add `--out FOLDER` to save somewhere else, or `--dll PATH` to use a different SimConnect.dll, such as the one from the MSFS 2024 SDK.

### How a run is detected

```mermaid
flowchart TD
    W(["Waiting"]) -->|"stopped on the runway,<br/>throttle at 90% or more"| A(["Armed"])
    A -->|"brakes released:<br/>ground speed passes 1 kt"| R(["Rolling"])
    R -->|"airborne for 0.5 s"| S[["Run saved"]]
    A -.->|"throttle below 90%"| W
    R -.->|"throttle below 80%,<br/>or no liftoff after 120 s"| C["Cancelled, nothing saved"]
```

1. Stop on the runway, hold the brakes and go to full throttle. The logger prints an **ARMED** line with the mass, flap angle, trim, wind, temperature, elevation and mixture: check them before you go.
2. Release the brakes. Recording starts once ground speed passes 1 kt.
3. Take off. The run is saved once the plane has stayed airborne for 0.5 s. Touching back down before that counts as a bounce: the run carries on and its `bounced` column says `yes`.

Don't pause the sim during a run. After a run is saved or cancelled, the logger waits for the next one.

## EE series

`ee_logger.py` labels every EE run using the elevation and temperature it measured:

| Series | Airport | Temperature |
| --- | --- | --- |
| `temperature` | The baseline airport (below 100 m, e.g. Ezeiza) | Any |
| `altitude` | A higher airport | The altitude-series temperature |
| `both` | The baseline airport | The altitude-series temperature, so it's the sea-level point of both series |
| `neither` | A higher airport | Anything else. The window warns you to fix it and redo the run |

The altitude-series temperature is 15 °C (±1 °C) unless you change it at the top of `ee_logger.py`:

| Setting | Default | What it does |
| --- | --- | --- |
| `BASELINE_MAX_ELEVATION_M` | 100 | Runs below this elevation count as the baseline airport |
| `ALTITUDE_SERIES_TEMP_C` | 15 | The temperature you hold fixed for the altitude series |
| `TEMP_TOLERANCE_C` | 1 | How far a run's temperature can be from that and still count |
| `ROTATE_IAS_KT` | 55 | Indicated airspeed at which it beeps during a run, so you know when to rotate |

During every EE run the logger beeps once when the indicated airspeed reaches 55 kt. The beep plays on its own thread, so it doesn't hold up the recording.

`EE/` also gets two extra files, rebuilt after every run and whenever the script starts:

- `temperature_series.csv`: the `temperature` and `both` runs, coldest first
- `altitude_series.csv`: the `altitude` and `both` runs, lowest first

To drop a bad run, delete its row from `EE/runs_summary.csv`. It disappears from the series files the next time you start the script.

## EE auto runs

`ee_auto.py` drives the sim through SimConnect, so every run starts the same way and there's no rotation technique in it. The plane never lifts off: each run ends at 60 KIAS, and the summary records how far it took to reach 55 KIAS instead of a ground roll.

Load into the flight, stop at the start of the runway with the engine running, set the weather, then start **Record EE auto runs.bat** (or `py ee_auto.py`, `--runs N` for N runs instead of 3). It then:

1. Puts 159 lb in each of the four seats and fills both wing tanks to 25.7 gal, which is what the sim's own "full fuel" loads. The mass has to read 2539 ± 2 lb, or it stops and says what's loaded.
2. Saves where the plane is standing as the start point.
3. Before each run, puts the plane back at the start point, sets the parking brake, flaps 10° and trim 13.6%, goes to full throttle and waits 3 s for the RPM to settle.
4. Checks the mass, flaps and wind (0.0 kt). If anything is off, it stops and says what's wrong instead of running.
5. Releases the parking brake and records every frame exactly like the normal logger.
6. At 60 KIAS it sets the throttle to idle and the parking brake on, and saves the run. After the last run it puts the plane back at the start point.

The weather isn't set by the script. MSFS 2024 doesn't let SimConnect change it: its SimConnect weather functions are deprecated, and the ambient temperature and wind variables can't be written. Pick the weather preset by hand as for manual runs.

Auto runs go into `EE/` with the same numbering, the same `run_NNN.csv` columns and the same series sorting as manual runs. Their `mode` column says `auto`, and the liftoff columns (and `bounced`) stay empty.

Every time `ee_auto.py` starts, it fills in the four new columns for runs that don't have them yet, from their `run_NNN.csv`. Manual runs saved by `ee_logger.py` leave these columns empty, so `py ee_auto.py --fill` fills them in without connecting to the sim. Values already in the file are never changed.

| Setting | Default | What it does |
| --- | --- | --- |
| `RUNS` | 3 | Runs in a row, unless `--runs` says otherwise |
| `TARGET_MASS_LB` | 2539 | The mass every run must have (± `MASS_TOLERANCE_LB`, 2) |
| `SEAT_WEIGHT_LB` | 159 | Weight in each of the four seats |
| `FULL_TANK_GAL` | 25.7 | Fuel in each wing tank |
| `TRIM_PCT` | 13.6 | Elevator trim, as in the manual runs |
| `RUN_UP_S` | 3 | Full throttle on the parking brake before each run |
| `MEASURE_IAS_KT` | 55 | Where the distance and TAS columns are measured |
| `END_IAS_KT` | 60 | Where the run ends |

## Output

Each folder gets:

- `runs_summary.csv`: one row per run
- `run_001.csv`, `run_002.csv`, …: every simulator frame of one run, from brake release to 0.5 s after liftoff

Numbering carries on from the highest run already in the folder. If Excel puts everything in one column, use **Data → From Text/CSV**, or open the file in Google Sheets.

### runs_summary.csv

| Column | Meaning |
| --- | --- |
| `run`, `file`, `recorded_at` | Run number, its frame file, and when it was saved |
| `mass_lb`, `mass_kg` | Total aircraft mass at brake release |
| `ground_roll_m` | Start point to liftoff point, measured from the aircraft's position |
| `ground_roll_check_m` | Same distance from integrating ground speed over time, as a cross-check |
| `roll_time_s` | Time from brake release to liftoff |
| `liftoff_ias_kt`, `liftoff_tas_kt`, `liftoff_gs_kt` | Indicated airspeed, true airspeed and ground speed at liftoff |
| `liftoff_aoa_deg`, `liftoff_pitch_deg` | Angle of attack and pitch at liftoff |
| `flaps_deg`, `trim_pct` | Flap angle and elevator trim at brake release |
| `max_rpm` | Highest engine RPM during the roll |
| `temp_c`, `pressure_hpa` | Mean outside air temperature and ambient pressure during the roll |
| `air_density_kg_m3` | Air density from those two: ρ = p / (R·T), R = 287.05 J/(kg·K) |
| `max_wind_kt` | Highest wind speed during the roll |
| `bounced` | `yes` if it touched back down before lifting off for good |
| `flaps_moved` | `yes` if the flaps moved more than 1° during the roll |
| `time_source` | `sim clock`, or `pc clock` if the sim's clock didn't tick every frame |
| `elevation_m` | Ground elevation at the start point |
| `mixture_pct` | Mixture lever position at brake release |
| `series`, `series_note` | EE only: the run's series, and why when it's `neither` |
| `dist_to_55kias_m` | EE only: brake release to where IAS passes 55 kt, interpolated between the frames either side |
| `check_dist_to_55kias_m` | EE only: the same distance from integrating ground speed over time |
| `tas_at_55kias_kt` | EE only: true airspeed where IAS passes 55 kt |
| `mode` | EE only: `auto` (ee_auto.py) or `manual` (ee_logger.py) |

Liftoff is taken as halfway between the last frame on the ground and the first frame in the air.

`elevation_m` and `mixture_pct` were added later. A summary file started by an older version, like the one in `IA/`, keeps its original columns, so new runs still line up with the old ones.

### run_NNN.csv

One row per simulator frame.

| Column | Meaning |
| --- | --- |
| `t_s` | Seconds since brake release |
| `dist_m` | Distance from the start point |
| `on_ground` | 1 on the ground, 0 in the air |
| `gs_kt`, `ias_kt`, `tas_kt` | Ground speed, indicated airspeed and true airspeed |
| `throttle_pct`, `rpm` | Throttle lever position and engine RPM |
| `pitch_deg`, `aoa_deg` | Pitch (nose-up positive) and angle of attack |
| `flaps_index`, `flaps_deg` | Flap handle position and flap angle |
| `trim_pct` | Elevator trim, as a percentage of full travel |
| `weight_lb` | Total aircraft weight |
| `temp_c`, `pressure_inhg` | Outside air temperature and ambient pressure |
| `wind_kt` | Wind speed |
| `lat_deg`, `lon_deg` | Aircraft position |
| `abs_time_s` | The simulator's absolute time |
| `ground_alt_m` | Ground elevation under the aircraft |
| `mixture_pct` | Mixture lever position |

## Settings

The recording thresholds sit at the top of `takeoff_logger.py` and apply to both investigations:

| Setting | Default | What it does |
| --- | --- | --- |
| `ARM_THROTTLE_PCT` | 90 | Throttle, while stopped, that arms a run |
| `START_SPEED_KT` | 1.0 | Ground speed that counts as brakes released |
| `ABORT_THROTTLE_PCT` | 80 | Throttle below this during the roll cancels the run |
| `AIRBORNE_CONFIRM_S` | 0.5 | How long the plane must stay airborne to count as liftoff |
| `MAX_ROLL_S` | 120 | Gives up if there's no liftoff after this long |
| `EXPECTED_FLAPS_DEG` | 10 | Warns when arming if the flaps aren't at this angle |
