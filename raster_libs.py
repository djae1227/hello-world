"""
Automatic LIBS raster: Standa XYZ stage (libximc) + SciAps Z903 handheld (HTTP Remote Service).

For every grid point:  move stage -> wait until stopped -> settle -> fire LIBS -> wait -> log -> next.

Before the first real run:
  1. Put the Z903 in "Remote Service" (swipe left on home screen -> Remote Service) and note its IP.
  2. Fill in the LIBS_* settings below from SciAps' "SA-NGL Remote Service" PDF / w_LIBS_example.py
     (Yen-Wei Lin, Aug 21 2024). The script will not fire until LIBS_SHOOT_PATH is set.
  3. Test pieces separately:
       python raster_libs.py --libs-test             # fire one shot, no stage
       python raster_libs.py --no-libs  -nx 3 -ny 3  # move stage only, no laser
       python raster_libs.py --simulate -nx 3 -ny 3  # no hardware at all

Example run (10 x 10 grid, 100 um spacing):
  python raster_libs.py -nx 10 -ny 10 --step-um 100
"""
import argparse
import csv
import datetime
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
# LIBS (SciAps Remote Service, HTTP) settings
# TODO: fill these in from the SciAps API PDF / w_LIBS_example.py
# ---------------------------------------------------------------------------
LIBS_IP = "192.168.0.207"      # IP shown on the Z903 Remote Service screen (USB tether is 192.168.42.129)
LIBS_PORT = None               # e.g. 8080 -- check the API doc / example script
LIBS_SHOOT_METHOD = "POST"     # GET, PUT or POST, per the API doc
LIBS_SHOOT_PATH = None         # e.g. "/api/..." -- the endpoint that starts a test/shot
LIBS_SHOOT_PAYLOAD = None      # dict sent as JSON body, if the endpoint needs one
LIBS_TIMEOUT_S = 30            # HTTP timeout; the shot request may block until the test finishes


def libs_base_url():
    port = f":{LIBS_PORT}" if LIBS_PORT else ""
    return f"http://{LIBS_IP}{port}"


class LibsClient:
    def __init__(self, simulate=False):
        self.simulate = simulate
        if simulate:
            return
        if not LIBS_SHOOT_PATH:
            sys.exit("LIBS_SHOOT_PATH is not set. Fill in the LIBS_* settings at the top of "
                     "raster_libs.py from the SciAps API doc, or run with --no-libs / --simulate.")
        import requests
        self.session = requests.Session()

    def shoot(self):
        """Fire one LIBS test. Returns a short string for the log."""
        if self.simulate:
            time.sleep(0.2)
            return "simulated"
        url = libs_base_url() + LIBS_SHOOT_PATH
        r = self.session.request(LIBS_SHOOT_METHOD, url, json=LIBS_SHOOT_PAYLOAD, timeout=LIBS_TIMEOUT_S)
        r.raise_for_status()
        return r.text[:200].replace("\n", " ")


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


def run(args):
    step_steps = round(args.step_um / UM_PER_STEP)
    if abs(step_steps * UM_PER_STEP - args.step_um) > 1e-9:
        print("Note: step rounded to {} um ({} motor steps)".format(step_steps * UM_PER_STEP, step_steps))
    z_steps = None if args.z_um is None else round(args.z_um / UM_PER_STEP)

    simulate_stage = args.simulate
    simulate_libs = args.simulate or args.no_libs
    libs = LibsClient(simulate=simulate_libs)
    stage = Stage(simulate=simulate_stage)

    total = args.nx * args.ny
    log_path = args.log or "raster_{}.csv".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    print("Raster {} x {} = {} spots, {} um spacing. Log: {}".format(
        args.nx, args.ny, total, step_steps * UM_PER_STEP, log_path))

    try:
        if not args.no_home:
            print("Homing...")
            stage.home_and_zero()
        if z_steps is not None:
            stage.move(0, 0, z_steps)

        with open(log_path, "w", newline="") as f:
            log = csv.writer(f)
            log.writerow(["shot", "i", "j", "x_um", "y_um", "z_um", "time", "libs_response"])
            for n, (i, j, x, y) in enumerate(raster_points(args.nx, args.ny, step_steps, not args.no_serpentine), 1):
                stage.move(x, y)
                time.sleep(args.settle_s)
                px, py, pz = stage.position()

                for attempt in range(1, args.retries + 2):
                    try:
                        resp = libs.shoot()
                        break
                    except Exception as e:
                        print("\nLIBS shot failed at ({}, {}), attempt {}: {}".format(i, j, attempt, e))
                        if attempt > args.retries:
                            raise
                        time.sleep(2)
                time.sleep(args.shot_wait_s)

                log.writerow([n, i, j, X_SIGN * px * UM_PER_STEP, py * UM_PER_STEP, pz * UM_PER_STEP,
                              datetime.datetime.now().isoformat(timespec="seconds"), resp])
                f.flush()
                print("Shot {}/{}  i={} j={}  X={:.1f} um  Y={:.1f} um".format(
                    n, total, i, j, X_SIGN * px * UM_PER_STEP, py * UM_PER_STEP), end="\r")
        print("\nRaster done.")

        stage.move(0, 0)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        print("Stop movement / disconnect device")
        stage.stop()
        stage.close()


def main():
    p = argparse.ArgumentParser(description="Automatic LIBS raster with Standa stage + SciAps Z903")
    p.add_argument("-nx", type=int, default=5, help="number of points in X")
    p.add_argument("-ny", type=int, default=5, help="number of points in Y")
    p.add_argument("--step-um", type=float, default=100.0, help="spacing between spots in um (multiple of 2.5)")
    p.add_argument("--z-um", type=float, default=None, help="move Z to this height (um) before rastering; default: leave Z")
    p.add_argument("--settle-s", type=float, default=0.5, help="pause after stage stops, before firing")
    p.add_argument("--shot-wait-s", type=float, default=2.0, help="pause after the LIBS request returns")
    p.add_argument("--retries", type=int, default=1, help="retries per spot if the LIBS request fails")
    p.add_argument("--no-serpentine", action="store_true", help="always scan rows left-to-right")
    p.add_argument("--no-home", action="store_true", help="skip homing (stage already zeroed)")
    p.add_argument("--no-libs", action="store_true", help="move the stage but do not fire")
    p.add_argument("--simulate", action="store_true", help="no hardware: fake stage and LIBS")
    p.add_argument("--libs-test", action="store_true", help="fire a single LIBS shot and exit")
    p.add_argument("--log", help="CSV log path")
    args = p.parse_args()

    if args.libs_test:
        print("LIBS response:", LibsClient(simulate=args.simulate).shoot())
        return
    run(args)


if __name__ == "__main__":
    main()
