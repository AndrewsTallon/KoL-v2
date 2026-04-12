CP210x USB-to-UART Virtual COM Port Driver (Silicon Labs)
==========================================================

The ESP32 sensor board used by KoL exposes its serial port through a
Silicon Labs CP2102 USB-to-UART bridge. On a Windows PC that does not
have the CP210x driver installed, the device appears in Device Manager
as "CP2102 USB to UART Bridge Controller" under "Other devices" with
status "The drivers for this device are not installed (Code 28)" --
and no COM port is ever enumerated, so the app cannot see the sensor.

This folder is where the driver files belong so that installer.iss can
bundle them and silently register them with `pnputil` during setup.

Setup (one time, on the build machine)
--------------------------------------

1. Download the "CP210x Universal Windows Driver" package from Silicon
   Labs' website. It ships as a ZIP named something like:

       CP210x_Universal_Windows_Driver.zip

2. Extract the ZIP into this folder (drivers\cp210x) so that the INF
   file sits directly inside it:

       drivers\cp210x\silabser.inf
       drivers\cp210x\silabser.cat
       drivers\cp210x\silabser.sys            (or arch subfolders)
       drivers\cp210x\x64\...
       drivers\cp210x\arm64\...
       ...

   The exact layout depends on the driver revision; the only hard
   requirement is that `silabser.inf` ends up at
   `drivers\cp210x\silabser.inf`.

3. Run `build_exe.bat`. It prints "[OK] CP210x driver found" if the
   INF was detected.

4. Compile the installer (Inno Setup). The installer now contains a
   [Run] step that executes:

       pnputil /add-driver drivers\cp210x\silabser.inf /install

   This stages the driver into the Windows Driver Store and installs
   it for the currently-connected CP2102 device, clearing the
   "Code 28" state without any user interaction.

Notes
-----

- `pnputil` is a built-in Windows tool (Windows 7+, always present on
  modern systems). No redistributable required.
- Driver installation is idempotent; re-running the installer on a
  machine that already has the driver is harmless.
- The driver binaries themselves are NOT committed to the Git
  repository (see `.gitignore`) because they are a third-party
  redistributable owned by Silicon Labs, and because they are ~2 MB.
- If you skip this step, the installer still compiles successfully,
  but `installer.iss` detects the missing INF and skips the pnputil
  step — end-user PCs will then need the driver installed some other
  way before the ESP32 sensor is visible.
