# Portable PWM Tests

This folder contains a portable PWM test package for SPIN.

Contents:
- `PWM/Test_SPIN_API/`: Python scripts and libraries
- `PWM/bench/`: automated execution through PlatformIO
- `requirements.txt`: Python dependencies
- `.env.example`: environment variable example

## Work Architecture

The test bench is split into three main blocks: the laptop that runs the tests, the `MUX` board that routes the signals, and the `CUT` board that generates the PWM signals to measure.

| Block | Role in the test | Code used | Controlled peripherals |
| --- | --- | --- | --- |
| Laptop | Runs the test scenarios, configures the CUT, configures the MUX, reads oscilloscope measurements, and decides `PASS` or `FAIL`. | `PWM/Test_SPIN_API/scripts/*.py`, `PWM/Test_SPIN_API/lib/*.py`, `PWM/bench/test/pwm/` | USB serial ports of the `CUT` and `MUX`, Rigol oscilloscope through VISA |
| MUX PCB | Routes the selected PWM signal from the CUT to an oscilloscope measurement channel. | `PWM/Firmware/MUX/src/main.cpp`, `PWM/Firmware/MUX/src/mux_control.h`, `PWM/Firmware/MUX/src/spin_data_objects.h` | Selection GPIOs `S0..S3`, enable GPIOs `MUX1..MUX3`, external multiplexers |
| CUT PCB | Generates the PWM signals requested by the tests. | `PWM/Firmware/CUT/src/main.cpp`, `PWM/Firmware/CUT/src/cut_pwm_control.h`, `PWM/Firmware/CUT/src/spin_data_objects.h` | PWM/HRTIM peripheral, PWM outputs such as `PA8`, `PA9`, `PA10`, `PB12..PB15`, `PC6..PC9` |

Simple flow view:

```text
Laptop scripts/bench
        |
        | serial ThingSet
        v
CUT PCB generates PWM
        |
        | physical PWM signal
        v
MUX PCB routes the signal
        |
        | measurement output
        v
Oscilloscope Rigol
        |
        | VISA
        v
Laptop checks the measurement
```

Recommended installation:

```powershell
cd .\Tests
python -m pip install -r requirements.txt
```

Direct script execution:

```powershell
cd .\PWM\Test_SPIN_API
python .\scripts\test_pwm_frequency.py --cut-port COM22 --mux-port COM20 --cut-pin PA8 --channel ch1 --freq-values 1000 5000 10000 --duty 0.50
```

Bench execution:

```powershell
cd .\PWM\bench
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
```

If `pio` is not available in `PATH`, use:

```powershell
py -m platformio test -e bench -f pwm/test_frequency
```

Prerequisites:
- Python
- PlatformIO
- accessible serial ports for the CUT and MUX
- Rigol oscilloscope accessible through VISA
