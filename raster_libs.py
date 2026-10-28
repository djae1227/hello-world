"""
Automatic LIBS raster: Standa XYZ stage (libximc) + SciAps Z903 handheld (HTTP Remote Service).

For every grid point:  move stage -> wait until stopped -> settle -> run one LIBS test -> save spectrum -> next.

The LIBS test is SciAps' "POST /data/raster?mode=<mode>" (SA-NGL Remote Service API). The request
returns only when the test has finished, with the averaged spectrum in the response, so the stage
never moves during a shot. The test uses the acquisition settings of that app on the handheld
(e.g. Geochem settings: locations, cleaning shots, data shots, argon).

Before the first real run:
  1. Z903: connect the PC to the handheld's hotspot (or the same Wi-Fi) and turn on Remote Service.
  2. Test pieces separately:
       python raster_libs.py --libs-test             # one LIBS test, no stage; saves libs_test_<time>.json
       python raster_libs.py --no-libs  -nx 3 -ny 3  # move stage only, no laser
       python raster_libs.py --simulate -nx 3 -ny 3  # no hardware at all

Example run (50 x 50 grid, 150 um spacing):
  python raster_libs.py -nx 50 -ny 50 --step-um 150

Output: raster_<date>.csv (one row per spot), raster_<date>.csv.json (grid settings) and
raster_<date>_spectra/shot_00001.json ... (the handheld's full response for each spot).

If the handheld drops off Wi-Fi or the laser needs to cool/warm up, the stage holds position and the
test is retried for up to --max-wait-min minutes. If the run stops for any reason, continue from the
next spot with:
  python raster_libs.py --resume raster_YYYYMMDD_HHMMSS.csv
"""
import argparse
import csv
import datetime
import json
import os
import socket
import sys
import time

# ---------------------------------------------------------------------------
# Stage settings (from the original stage script)
# ---------------------------------------------------------------------------
#These ports are subject to change, make sure that you choose the appropriate port for you
DEVICE_URI_X = r"xi-com:\\.\COM5"
DEVICE_URI_Y = r"xi-com:\\.\COM4"
DEVICE_URI_Z = r"xi-com:\\.\COM3"
UM_PER_STEP = 2.5        # motor step results in 2.5um translation according to manufacturer
Z_START_STEPS = -3000    # Z offset applied after homing, before zeroing (same as original script)
X_SIGN = -1              # original script moved X in the negative direction

# ---------------------------------------------------------------------------
# LIBS (SciAps Remote Service, HTTP) settings -- from SA-NGL Remote Service PDF / w_LIBS_example.py
# ---------------------------------------------------------------------------
LIBS_IP = "192.168.43.1"   # Z903 hotspot address (USB tether would be 192.168.42.129)
LIBS_PORT = 8080
LIBS_MODE = "Geochem"      # Alloy, AlloyPlus, Carbon, Geochem, Empirical, ... (app whose settings are used)
LIBS_TIMEOUT_S = 180       # the raster request blocks until the whole test is done

# Status codes from /data/raster, sorted by what the script should do about them
STATUS_OK = "CODE_SUCCESS"
STATUS_RETRY = {"CODE_LASER_NOT_READY", "CODE_LASER_OVERHEATING", "CODE_LASER_NEEDS_WARM_UP", "CODE_TIMEOUT"}
STATUS_SKIP = {"CODE_NO_SAMPLE_DETECTED", "CODE_AIRSHOT_AUTO_ABORT"}   # log the spot and move on
# anything else (CODE_LOW_ARGON, CODE_ABORT, CODE_INVALID_CONFIG, CODE_WRONG_STAGE, ...) stops the run


def libs_base_url():
    return "http://{}:{}".format(LIBS_IP, LIBS_PORT)


class LibsStop(Exception):
    """The handheld reported something a person has to fix; stop and save progress."""


class LibsRetry(Exception):
    """Temporary condition (laser warming up / cooling down); retry the same spot."""


class LibsClient:
    def __init__(self, mode=LIBS_MODE, all_spectra=False, api="data", simulate=False):
        self.mode = mode
        self.all_spectra = all_spectra
        self.api = api              # "data": /data/raster test,  "hardware": arm + /hardware/go
        self.armed = False
        self.simulate = simulate
        if not simulate:
            import requests
            self.session = requests.Session()

    def reachable(self, timeout_s=3):
        """True if the analyzer's Remote Service port accepts a TCP connection."""
        if self.simulate:
            return True
        try:
            socket.create_connection((LIBS_IP, LIBS_PORT), timeout=timeout_s).close()
            return True
        except OSError:
            return False

    def settings(self):
        """Acquisition settings the handheld will use for this mode."""
        if self.simulate:
            return {"mNumLocations": 1, "mNumCleaningPerLocation": 12, "mNumDataPerLocation": 16, "mUseArgon": False}
        r = self.session.get(libs_base_url() + "/instrument/acquisitionParams",
                             params={"mode": self.mode}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _check(self, r):
        """Raise LibsStop right away if the handheld does not support this request (no point retrying)."""
        if r.status_code in (400, 404, 405, 501):
            hint = (" Try --api hardware (arm + /hardware/go) instead." if self.api == "data" else "")
            raise LibsStop("handheld does not support {} {} (HTTP {} {}).{}".format(
                r.request.method, r.url, r.status_code, r.reason, hint))
        r.raise_for_status()

    def shoot(self):
        """Run one LIBS test. Returns the handheld's JSON response (status, metadata, spectra)."""
        if self.simulate:
            time.sleep(0.2)
            return {"status": STATUS_OK, "abortedByUser": "false", "results": []}
        if self.api == "hardware":
            return self._shoot_hardware()
        path = "/data/raster/all" if self.all_spectra else "/data/raster"
        r = self.session.post(libs_base_url() + path, params={"mode": self.mode}, timeout=LIBS_TIMEOUT_S)
        self._check(r)
        return r.json()

    def hw_get(self, path):
        """GET a hardware register (e.g. /hardware/micro/142) and return its integer value."""
        if self.simulate:
            return self._sim_regs.get(path, 20) if hasattr(self, "_sim_regs") else 20
        r = self.session.get(libs_base_url() + path, timeout=10)
        self._check(r)
        return r.json()["value"]

    def hw_put(self, path, value):
        if self.simulate:
            self._sim_regs = getattr(self, "_sim_regs", {})
            self._sim_regs[path] = value
            return
        r = self.session.put(libs_base_url() + "{}/{}".format(path, int(value)), timeout=10)
        self._check(r)
        if r.json().get("status") != "success":
            raise LibsStop("handheld did not accept {} = {}".format(path, value))

    def _shoot_hardware(self):
        """Low-level API: arm the laser once, then POST /hardware/go (fires with the unit's current settings)."""
        if not self.armed:
            self._check(self.session.put(libs_base_url() + "/hardware/armlaser/1", timeout=10))
            self.armed = True
        r = self.session.post(libs_base_url() + "/hardware/go", timeout=LIBS_TIMEOUT_S)
        self._check(r)
        resp = r.json()
        if resp.get("status") != "success":
            self.armed = False      # re-arm before the retry
            raise LibsRetry("hardware/go returned {}".format(resp.get("status")))
        resp["status"] = STATUS_OK
        return resp

    def shoot_until_ok(self, max_wait_min):
        """
        Run one test at the current spot. Network failures and temporary laser states are retried with
        increasing pauses (stage holding position) for up to max_wait_min minutes.
        Returns (response, attempts). Raises LibsStop for conditions a person has to fix.
        """
        deadline = time.time() + max_wait_min * 60
        attempt, pause = 0, 2
        while True:
            attempt += 1
            try:
                resp = self.shoot()
                status = resp.get("status")
                if str(resp.get("abortedByUser")).lower() == "true":
                    raise LibsStop("test aborted on the handheld screen")
                if status in STATUS_RETRY:
                    raise LibsRetry(status)
                if status != STATUS_OK and status not in STATUS_SKIP:
                    raise LibsStop("handheld reported {}".format(status))
                return resp, attempt
            except LibsStop:
                raise
            except Exception as e:
                if time.time() > deadline:
                    raise LibsStop("LIBS not working for {} min: {}".format(max_wait_min, e))
                print("\n[{}] LIBS test failed (attempt {}): {}. Waiting {} s, stage holding position...".format(
                    datetime.datetime.now().strftime("%H:%M:%S"), attempt, e, pause))
                time.sleep(pause)
                while not self.reachable() and time.time() < deadline:
                    time.sleep(5)
                pause = min(pause * 2, 60)


# ---------------------------------------------------------------------------
# Stage (libximc)
# ---------------------------------------------------------------------------
class Stage:
    def __init__(self, simulate=False):
        self.simulate = simulate
        self.pos = [0, 0, 0]
        if simulate:
            return
        global ximc, byref, c_int
        from ctypes import byref, c_int
        import libximc as ximc
        self.ids = []
        for uri in (DEVICE_URI_X, DEVICE_URI_Y, DEVICE_URI_Z):
            dev = ximc.lib.open_device(uri.encode())
            if dev <= 0:
                self.close()
                raise RuntimeError("Failed to open device with URI", uri)
            print("Device with URI {} successfully opened".format(uri))
            self.ids.append(dev)

    def home_and_zero(self):
        """Makes the stages calibrate their 0,0,0 position as it doesn't have absolute position knowledge without it"""
        if self.simulate:
            return
        for dev in self.ids:
            ximc.lib.command_home(dev)
        self.wait_for_stop()
        ximc.lib.command_move(self.ids[2], c_int(Z_START_STEPS))
        self.wait_for_stop()
        for dev in self.ids:
            ximc.lib.command_zero(dev)

    def move(self, x_steps, y_steps, z_steps=None):
        if self.simulate:
            self.pos = [x_steps, y_steps, self.pos[2] if z_steps is None else z_steps]
            time.sleep(0.05)
            return
        ximc.lib.command_move(self.ids[0], c_int(int(x_steps)))
        ximc.lib.command_move(self.ids[1], c_int(int(y_steps)))
        if z_steps is not None:
            ximc.lib.command_move(self.ids[2], c_int(int(z_steps)))
        self.wait_for_stop()

    def position(self):
        """Current (x, y, z) in steps."""
        if self.simulate:
            return tuple(self.pos)
        out = []
        for dev in self.ids:
            p = ximc.get_position_t()
            if ximc.lib.get_position(dev, byref(p)) != ximc.Result.Ok:
                raise RuntimeError("get_position failed")
            out.append(p.Position)
        return tuple(out)

    def wait_for_stop(self, msec=10, timeout_s=60):
        """Poll all three axes until none reports MVCMD_RUNNING."""
        t0 = time.time()
        while True:
            running = False
            for dev in self.ids:
                st = ximc.status_t()
                if ximc.lib.get_status(dev, byref(st)) != ximc.Result.Ok:
                    raise RuntimeError("get_status failed")
                if st.MvCmdSts & ximc.MvcmdStatus.MVCMD_RUNNING:
                    running = True
            if not running:
                return
            if time.time() - t0 > timeout_s:
                raise TimeoutError("Stage did not stop within {} s".format(timeout_s))
            ximc.lib.msec_sleep(msec)

    def stop(self):
        if self.simulate:
            return
        for dev in self.ids:
            ximc.lib.command_stop(dev)

    def close(self):
        if self.simulate:
            return
        for dev in self.ids:
            ximc.lib.close_device(byref(c_int(dev)))
        self.ids = []


# ---------------------------------------------------------------------------
# Raster
# ---------------------------------------------------------------------------
def raster_points(nx, ny, step_steps, serpentine=True):
    """Yield (i, j, x_steps, y_steps). Serpentine rows avoid the long return move on every row."""
    for j in range(ny):
        cols = range(nx)
        if serpentine and j % 2 == 1:
            cols = reversed(cols)
        for i in cols:
            yield i, j, X_SIGN * i * step_steps, j * step_steps


LOG_HEADER = ["shot", "i", "j", "x_um", "y_um", "z_um", "time", "attempts", "status",
              "avgshots", "laser_temp", "argon_psi", "spectrum_file"]


def load_resume(log_path):
    """Return (grid settings, number of shots already completed) from a previous run's log + .json."""
    with open(log_path + ".json") as f:
        grid = json.load(f)
    done = 0
    with open(log_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("shot", "").isdigit():
                done = int(row["shot"])
    return grid, done


def describe_data(resp):
    """Short summary of how much spectrum data a response holds."""
    data = resp.get("data")
    if data is None and resp.get("results"):
        data = resp["results"][0].get("spectrum", {}).get("mPixels")
    if not data:
        return "NONE (empty)"
    rows = [r for r in data if isinstance(r, list)]
    if not rows:
        return "{} values".format(len(data))
    nonzero = sum(1 for r in rows for v in r if v)
    return "{} array(s) x {} values ({} non-zero)".format(len(rows), len(rows[0]), nonzero)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())


# Hardware registers from the SA-NGL Remote Service PDF (used with --api hardware)
REG_PULSES = "/hardware/micro/142"     # pulses per location (micro register 0x8E)
REG_LOCATIONS = "/hardware/fpga/6"     # test location count (FPGA register 6)


def configure_hardware(libs, pulses=None, locations=None):
    """Print the pulse/location registers, set them if asked, and read back to confirm."""
    for name, reg, want in (("pulses per location", REG_PULSES, pulses),
                            ("locations per fire", REG_LOCATIONS, locations)):
        old = libs.hw_get(reg)
        if want is None or want == old:
            print("Handheld {}: {}".format(name, old))
            continue
        libs.hw_put(reg, want)
        new = libs.hw_get(reg)
        if new != want:
            raise LibsStop("tried to set {} to {} but the handheld reports {}".format(name, want, new))
        print("Handheld {}: {} -> {}  (to restore later: --{} {})".format(
            name, old, new, "pulses" if reg == REG_PULSES else "locations", old))


def setup_libs(libs, pulses=None, locations=None):
    """Show (and for --api hardware, set) the handheld's shot settings before firing."""
    try:
        if libs.api == "hardware":
            configure_hardware(libs, pulses, locations)
        else:
            if pulses is not None or locations is not None:
                sys.exit("--pulses / --locations only work with --api hardware.")
            print_settings(libs)
    except LibsStop as e:
        sys.exit("Could not set up the handheld: {}".format(e))


def print_settings(libs):
    try:
        s = libs.settings()
    except Exception as e:
        print("(Could not read the handheld's {} settings -- not supported by this firmware: {}. Continuing.)".format(
            libs.mode, str(e).split(" for url")[0]))
        return
    print("Handheld {} settings: {} location(s) per test, {} cleaning + {} data shots per location, argon {}".format(
        libs.mode, s.get("mNumLocations"), s.get("mNumCleaningPerLocation"), s.get("mNumDataPerLocation"),
        "on" if s.get("mUseArgon") else "off"))
    if (s.get("mNumLocations") or 1) > 1:
        print("  WARNING: the handheld moves its own internal stage to {} locations per test, so each grid point "
              "gets {} spots, not 1. Set Locations to 1 in the {} settings on the handheld for one spot per point."
              .format(s.get("mNumLocations"), s.get("mNumLocations"), libs.mode))


def require_reachable(libs):
    if not libs.reachable():
        sys.exit("Cannot reach the handheld at {}.\n"
                 "  - On the handheld, open Remote Service and check it shows this IP (tap the gear icon to start it).\n"
                 "  - Check the PC is on the handheld's Wi-Fi/hotspot.\n"
                 "  - If Remote Service shows a different IP, add --ip <that IP> to the command.".format(libs_base_url()))


def run(args):
    if args.resume:
        log_path = args.resume
        grid, done = load_resume(log_path)
        print("Resuming {}: {} shots already done.".format(log_path, done))
    else:
        log_path = args.log or "raster_{}.csv".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        grid = {"nx": args.nx, "ny": args.ny, "step_um": args.step_um, "z_um": args.z_um,
                "serpentine": not args.no_serpentine, "mode": args.mode, "all_spectra": args.all_spectra,
                "api": args.api, "pulses": args.pulses, "locations": args.locations}
        done = 0

    step_steps = round(grid["step_um"] / UM_PER_STEP)
    if abs(step_steps * UM_PER_STEP - grid["step_um"]) > 1e-9:
        print("Note: step rounded to {} um ({} motor steps)".format(step_steps * UM_PER_STEP, step_steps))
    z_steps = None if grid["z_um"] is None else round(grid["z_um"] / UM_PER_STEP)
    total = grid["nx"] * grid["ny"]
    if done >= total:
        print("All {} shots already done.".format(total))
        return

    libs = LibsClient(mode=grid.get("mode", LIBS_MODE), all_spectra=grid.get("all_spectra", False),
                      api=grid.get("api", "data"),
                      simulate=args.simulate or args.no_libs)
    require_reachable(libs)
    setup_libs(libs, grid.get("pulses"), grid.get("locations"))
    stage = Stage(simulate=args.simulate)

    spectra_dir = os.path.splitext(log_path)[0] + "_spectra"
    os.makedirs(spectra_dir, exist_ok=True)
    print("Raster {} x {} = {} spots, {} um spacing.".format(grid["nx"], grid["ny"], total, step_steps * UM_PER_STEP))
    print("Log:     {}\nSpectra: {}".format(os.path.abspath(log_path), os.path.abspath(spectra_dir)))

    skipped_in_a_row = 0
    try:
        # Homing gives the same absolute zero every time, so a resumed run lands on the same grid.
        if not args.no_home:
            print("Homing...")
            stage.home_and_zero()
        if z_steps is not None:
            stage.move(0, 0, z_steps)

        if not args.resume:
            with open(log_path + ".json", "w") as f:
                json.dump(grid, f, indent=2)
        with open(log_path, "a" if args.resume else "w", newline="") as f:
            log = csv.writer(f)
            if not args.resume:
                log.writerow(LOG_HEADER)
            points = list(raster_points(grid["nx"], grid["ny"], step_steps, grid["serpentine"]))
            for n, (i, j, x, y) in enumerate(points[done:], done + 1):
                stage.move(x, y)
                time.sleep(args.settle_s)
                px, py, pz = stage.position()

                resp, attempts = libs.shoot_until_ok(args.max_wait_min)
                if resp.get("avgshots") == 0:
                    raise LibsStop("the handheld returned 0 averaged spectra (avgshots = 0), so there is no data. "
                                   "Increase --locations.")
                time.sleep(args.shot_wait_s)

                status = resp.get("status")
                skipped_in_a_row = skipped_in_a_row + 1 if status in STATUS_SKIP else 0
                spectrum_file = "shot_{:05d}.json".format(n)
                save_json(os.path.join(spectra_dir, spectrum_file), resp)
                meta = resp.get("metadata", {})
                log.writerow([n, i, j, X_SIGN * px * UM_PER_STEP, py * UM_PER_STEP, pz * UM_PER_STEP,
                              datetime.datetime.now().isoformat(timespec="seconds"), attempts, status,
                              resp.get("avgshots", ""), meta.get("mMaxLaserTemp", ""), meta.get("mArgonPSI", ""), spectrum_file])
                f.flush()
                os.fsync(f.fileno())
                print("Shot {}/{}  i={} j={}  X={:.1f} um  Y={:.1f} um  {}".format(
                    n, total, i, j, X_SIGN * px * UM_PER_STEP, py * UM_PER_STEP, status), end="\r")

                if status in STATUS_SKIP:
                    print("\nSpot {} ({}, {}): {} -- logged, moving on.".format(n, i, j, status))
                    if skipped_in_a_row >= args.max_skips:
                        raise LibsStop("{} spots in a row returned {}; check the sample / sample detection".format(
                            skipped_in_a_row, status))
        print("\nRaster done. Data in {}".format(os.path.abspath(spectra_dir)))

        stage.move(0, 0)
    except (KeyboardInterrupt, LibsStop, RuntimeError, TimeoutError) as e:
        print("\nStopped: {}".format(e or "interrupted by user"))
        if os.path.exists(log_path + ".json"):
            print("Progress saved. To continue from the next spot run:\n  python raster_libs.py --resume {}".format(log_path))
    finally:
        print("Stop movement / disconnect device")
        stage.stop()
        stage.close()


def main():
    global LIBS_IP, LIBS_PORT
    p = argparse.ArgumentParser(description="Automatic LIBS raster with Standa stage + SciAps Z903")
    p.add_argument("-nx", type=int, default=5, help="number of points in X")
    p.add_argument("-ny", type=int, default=5, help="number of points in Y")
    p.add_argument("--step-um", type=float, default=100.0, help="spacing between spots in um (multiple of 2.5)")
    p.add_argument("--z-um", type=float, default=None, help="move Z to this height (um) before rastering; default: leave Z")
    p.add_argument("--pulses", type=int, help="(--api hardware) micro register 142; had no effect on the lab Z903")
    p.add_argument("--locations", type=int,
                   help="(--api hardware) FPGA register 6; on the lab Z903 avgshots = locations / 4 (36 -> 9, 1 -> 0)")
    p.add_argument("--api", choices=["data", "hardware"], default="data",
                   help="data: full test via /data/raster (default); hardware: arm laser + /hardware/go")
    p.add_argument("--mode", default=LIBS_MODE, help="handheld app whose acquisition settings are used (default Geochem)")
    p.add_argument("--all-spectra", action="store_true", help="save every shot's spectrum, not just the average")
    p.add_argument("--settle-s", type=float, default=0.5, help="pause after stage stops, before firing")
    p.add_argument("--shot-wait-s", type=float, default=0.5, help="pause after the LIBS test returns, before moving")
    p.add_argument("--max-wait-min", type=float, default=30,
                   help="if the handheld drops off or the laser is not ready, keep retrying this long (progress is saved)")
    p.add_argument("--max-skips", type=int, default=5,
                   help="stop if this many spots in a row report no sample / air shot")
    p.add_argument("--resume", metavar="LOG_CSV", help="continue an interrupted run from its log file")
    p.add_argument("--no-serpentine", action="store_true", help="always scan rows left-to-right")
    p.add_argument("--no-home", action="store_true", help="skip homing (stage already zeroed)")
    p.add_argument("--no-libs", action="store_true", help="move the stage but do not fire")
    p.add_argument("--simulate", action="store_true", help="no hardware: fake stage and LIBS")
    p.add_argument("--libs-test", action="store_true", help="run a single LIBS test, save libs_test_<time>.json and exit")
    p.add_argument("--log", help="CSV log path")
    p.add_argument("--ip", help="handheld IP shown on its Remote Service screen (default {})".format(LIBS_IP))
    p.add_argument("--port", type=int, help="Remote Service port (default {})".format(LIBS_PORT))
    args = p.parse_args()

    LIBS_IP = args.ip or LIBS_IP
    LIBS_PORT = args.port or LIBS_PORT

    if args.libs_test:
        libs = LibsClient(mode=args.mode, all_spectra=args.all_spectra, api=args.api, simulate=args.simulate)
        require_reachable(libs)
        setup_libs(libs, args.pulses, args.locations)
        t0 = time.time()
        try:
            resp = libs.shoot()
        except LibsStop as e:
            sys.exit("LIBS test failed: {}".format(e))
        name = "libs_test_{}{}.json".format(
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            "_loc{}".format(args.locations) if args.locations is not None else "")
        save_json(name, resp)
        print("Status: {}  ({:.1f} s)".format(resp.get("status"), time.time() - t0))
        if "avgshots" in resp:
            print("avgshots (averaged spectra reported by the handheld): {}".format(resp["avgshots"]))
        print("Spectrum data: {}".format(describe_data(resp)))
        print("Saved to: {}".format(os.path.abspath(name)))
        return
    run(args)


if __name__ == "__main__":
    main()
