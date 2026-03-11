"""
KoL Adaptive Lighting — Packaged Application Launcher

Entry point for the PyInstaller-built executable.
Starts the backend web server and opens the browser dashboard.
"""

import argparse
import logging
import sys
import threading
import time
import webbrowser


def main():
    parser = argparse.ArgumentParser(
        description="KoL Adaptive Lighting Control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  KoL.exe --sensor-port COM3\n"
            "  KoL.exe --sensor-port COM3 --mode ai\n"
            "  KoL.exe --dry-run                  (no hardware needed)\n"
        ),
    )
    parser.add_argument(
        "--sensor-port",
        default=None,
        help="Serial port for ESP32 sensor (e.g. COM3, COM4). "
             "Required unless --dry-run is used.",
    )
    parser.add_argument("--sensor-baud", type=int, default=115200)
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate hardware (no USB devices needed)")
    parser.add_argument("--mode", choices=["manual", "ai"], default="manual")
    parser.add_argument("--web-port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open the browser")

    args = parser.parse_args()

    if not args.sensor_port and not args.dry_run:
        parser.error("--sensor-port is required (e.g. --sensor-port COM3). "
                     "Use --dry-run to run without hardware.")

    # Build the argv that dalicontrol.main expects
    sys.argv = ["KoL"]
    if args.sensor_port:
        sys.argv += ["--sensor-port", args.sensor_port]
    elif args.dry_run:
        # Provide a dummy port for the required arg; dry-run won't use it
        sys.argv += ["--sensor-port", "NONE"]
    sys.argv += ["--sensor-baud", str(args.sensor_baud)]
    sys.argv += ["--mode", args.mode]
    sys.argv += ["--web", "--no-cli"]
    sys.argv += ["--web-port", str(args.web_port)]
    if args.dry_run:
        sys.argv.append("--dry-run")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Open browser after a short delay (server needs a moment to start)
    if not args.no_browser:
        url = f"http://localhost:{args.web_port}"

        def open_browser():
            time.sleep(2)
            logging.info("Opening browser: %s", url)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    # Import and run the actual application
    from dalicontrol.main import main as app_main
    app_main()


if __name__ == "__main__":
    main()
