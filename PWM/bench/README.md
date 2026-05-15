# PWM Bench Runner

This folder contains the PlatformIO native bench runner used to execute the
PWM hardware validation scripts.

The bench runner calls the Python scripts located in
`../Test_SPIN_API/scripts`.

## Structure

```text
bench/
  platformio.ini
  runners/
    common.py
  test/
    pwm/
      test_burst_mode/
      test_dead_time/
      test_duty_cycle/
      test_frequency/
      test_modulation_modes/
      test_phase_shift/
      test_resolution/
      test_rise_fall/
      test_switch_convention/
```

## Before Running Tests

After flashing the CUT board, wait a few seconds and check that the USB serial
ports are visible again:

```powershell
Start-Sleep -Seconds 5
pio device list
```

If the CUT ports, for example `COM21` and `COM22`, are not visible, press the
physical reset button or unplug/replug the CUT board, then check again with
`pio device list`.

Default ports are configured in each `bench_config.json`:

```text
CUT: COM22
MUX: COM20
```

Override them from PowerShell when needed:

```powershell
$env:BENCH_CUT_PORT="COMxx"
$env:BENCH_MUX_PORT="COMyy"
```

## Run Tests

Run commands from this folder:

```powershell
cd C:\PFEOwnTech\SPOC_Test_Software\PWM\bench
pio test -e bench -f pwm/test_frequency
```

Available test filters:

```powershell
pio test -e bench -f pwm/test_duty_cycle
pio test -e bench -f pwm/test_frequency
pio test -e bench -f pwm/test_rise_fall
pio test -e bench -f pwm/test_dead_time
pio test -e bench -f pwm/test_phase_shift
pio test -e bench -f pwm/test_burst_mode
pio test -e bench -f pwm/test_modulation_modes
pio test -e bench -f pwm/test_switch_convention
pio test -e bench -f pwm/test_resolution
```

The resolution test checks one `initVariableFrequency` minimum-frequency value per
CUT initialization, then routes the PWM output through the MUX and checks the
frequency on the Rigol. To check another prescaler, set one value and
reset/restart the CUT before running again:

```powershell
$env:BENCH_RES_FREQ_VALUES="8000 10000 20000 50000 100000"
pio test -e bench -f pwm/test_resolution
```

When `BENCH_RES_MIN_FREQ` is not set, the test uses the first
`BENCH_RES_FREQ_VALUES` entry as the minimal frequency.
