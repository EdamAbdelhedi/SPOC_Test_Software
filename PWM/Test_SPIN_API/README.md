# Test SPIN API PWM

This folder contains the Python PWM verification scripts for SPIN.

Contents:
- `scripts/`: PWM test scripts
- `lib/`: shared classes used to control the CUT, the MUX, and the oscilloscope
- `PWM_Architecture.md`: overview of the test architecture

Available suites:
- `test_pwm_burst_mode.py`
- `test_pwm_dead_time.py`
- `test_pwm_duty_cycle.py`
- `test_pwm_frequency.py`
- `test_pwm_modulation_modes.py`
- `test_pwm_phase_shift.py`
- `test_pwm_rise_fall.py`

Each script uses the classes from `lib/`:
- `cut_pwm.py`
- `mux_controller.py`
- `oscilloscope.py`
- `thingset.py`

Hardware requirements:
- a CUT board available on a serial port
- a MUX board available on a serial port
- a Rigol oscilloscope available through VISA

Install Python dependencies before running scripts directly:

```powershell
python -m pip install -r ..\..\requirements.txt
```

Run a script directly:

```powershell
python .\scripts\test_pwm_duty_cycle.py --cut-port COM22 --mux-port COM20 --cut-pin PA8 --channel ch1 --freq 1000 --duty-values 0.20 0.70
```

Burst mode example on a single pin:

```powershell
python .\scripts\test_pwm_burst_mode.py --cut-port COM22 --mux-port COM20 --all-pwm-pins false --cut-pin PA8 --channel ch2 --freq 1000 --duty 0.50 --burst-off-cycles 7 --burst-total-cycles 10
```

Automated run through the PlatformIO bench:

```powershell
cd ..\bench
$env:BENCH_CUT_PORT = "COM22"
$env:BENCH_MUX_PORT = "COM20"
```

Available commands:

```powershell
pio test -e bench -f pwm/test_duty_cycle
pio test -e bench -f pwm/test_frequency
pio test -e bench -f pwm/test_rise_fall
pio test -e bench -f pwm/test_dead_time
pio test -e bench -f pwm/test_phase_shift
pio test -e bench -f pwm/test_burst_mode
pio test -e bench -f pwm/test_modulation_modes
```

To run burst mode on a single pin through the bench:

```powershell
$env:BENCH_BURST_ALL_PWM_PINS = "false"
$env:BENCH_BURST_CUT_PIN = "PA8"
pio test -e bench -f pwm/test_burst_mode
```

If `pio` is not available in `PATH`, use:

```powershell
py -m platformio test -e bench -f pwm/test_frequency
```

The bench environment variables are documented in the neighboring `../bench` folder.
