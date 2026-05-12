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
      test_rise_fall/
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
```
