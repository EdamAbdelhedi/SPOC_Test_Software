#!/usr/bin/env python3
"""End-to-end CUT + MUX + BNC burst-mode check built on the reusable classes."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR.parent / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from cut_pwm import CutPwmController, PWM_SOURCES, clamp_pwm_duty, resolve_pwm_frequency_hz  # type: ignore  # noqa: E402
from mux_controller import MuxController  # type: ignore  # noqa: E402
from oscilloscope import Oscilloscope  # type: ignore  # noqa: E402

SUPPORTED_PWM_PINS = tuple(sorted(PWM_SOURCES.keys()))
ALL_PWM_PINS = ("PA8", "PA9", "PA10", "PB12", "PB13", "PB14", "PB15", "PC6", "PC7", "PC8", "PC9")
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route one CUT PWM signal through MUX to the BNC and measure burst-mode behavior"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin", default="PA8", help="PWM-capable CUT pin name, for example PA8")
    parser.add_argument(
        "--all-pwm-pins",
        default="false",
        help="When true, run the burst-mode test on every built-in PWM-capable CUT pin",
    )
    parser.add_argument("--channel", default="ch1", help="Shield channel name, for example ch1")
    parser.add_argument("--freq", type=int, default=1000, help="Carrier PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.50, help="Carrier PWM duty value as a fraction")
    parser.add_argument(
        "--burst-off-cycles",
        "--bm-cmp",
        dest="burst_cmp",
        type=int,
        default=7,
        help="OwnTech bm_cmp: number of carrier periods forced off inside the burst cycle",
    )
    parser.add_argument(
        "--burst-total-cycles",
        "--bm-per",
        dest="burst_per",
        type=int,
        default=10,
        help="OwnTech bm_per: total number of carrier periods in one burst cycle",
    )
    parser.add_argument("--settle-seconds", type=float, default=0.1, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--burst-timebase-scale", type=float, help="Burst-envelope measurement timebase in seconds/div")
    parser.add_argument("--freq-tol-pct", type=float, default=5.0, help="Allowed carrier frequency error in percent")
    parser.add_argument("--duty-tol-pct", type=float, default=3.0, help="Allowed carrier duty error in percent")
    parser.add_argument(
        "--burst-period-tol-pct",
        type=float,
        default=5.0,
        help="Allowed burst repetition period error in percent",
    )
    return parser


def default_burst_timebase(freq_hz: int, burst_per: int) -> float:
    return max(5.0 / max(float(freq_hz), 1.0), 1e-6)


def default_carrier_timebase(freq_hz: int, burst_per: int) -> float:
    return default_burst_timebase(freq_hz, burst_per)


def normalize_cut_pin(cut_pin: str) -> str:
    normalized = cut_pin.strip().upper()
    if normalized not in PWM_SOURCES:
        raise ValueError(
            f"Unknown PWM-capable CUT pin '{cut_pin}'. Supported pins: {', '.join(SUPPORTED_PWM_PINS)}"
        )
    return normalized


def normalize_channel(channel: str) -> str:
    normalized = channel.strip().lower()
    if normalized not in CHANNEL_TO_SCOPE:
        raise ValueError(f"Unknown channel '{channel}'. Expected one of: {', '.join(CHANNEL_TO_SCOPE)}")
    return normalized


def parse_bool_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'. Use true/false.")


def normalize_duty_percent(raw: float) -> float:
    return raw * 100.0 if raw <= 1.0 else raw


def pct_error(measured: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if abs(measured) < 1e-12 else float("inf")
    return abs(measured - expected) / abs(expected) * 100.0


def prepare_scope(
    scope: Oscilloscope,
    scope_channel: int,
    *,
    timebase_scale: float,
    trigger_edge_level: float,
) -> None:
    scope.configure_calibrated_channel(
        scope_channel,
        channel_probe=SCOPE_CHANNEL_PROBE,
        channel_scale=SCOPE_CHANNEL_SCALE,
        offset_volts=0.0,
        coupling="DC",
        enabled=True,
    )
    scope.set_timebase(timebase_scale)
    scope.configure_edge_trigger(channel=scope_channel, trigger_edge_level=trigger_edge_level, slope="POS")
    scope.run()


def average_valid_measurements(
    reader,
    *,
    averages: int,
    settle_seconds: float,
) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            values.append(float(reader()))
        except Exception:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def calculate_burst_active_pulses_time_s(*, pulse_count: int, duty: float, period_s: float) -> float:
    if pulse_count <= 0:
        return 0.0
    high_time_s = pulse_count * duty * period_s
    inter_pulse_low_time_s = (pulse_count - 1) * (1.0 - duty) * period_s
    return high_time_s + inter_pulse_low_time_s


def print_route_diagnostics(cut: CutPwmController, mux: MuxController, channel: str, cut_pin: str) -> None:
    cut_state = cut.read_state()
    mux_state = mux.read_route(channel=channel, cut_pin=cut_pin)
    pwm_source = PWM_SOURCES[cut_pin]
    print(
        f"route {cut_pin} -> CUT(PWM{pwm_source['unit']}{pwm_source['output']}) "
        f"via {mux_state.mux}/{mux_state.channel}"
    )
    print(
        "readback "
        f"CUT(enabled={cut_state.enabled}, outputs={list(cut_state.enabled_outputs)}, rStatus={cut_state.status}) "
        f"MUX(channel={mux_state.channel}, mux={mux_state.mux}, rChannel={mux_state.input_index}, rEnable={mux_state.enabled})"
    )


def check_pwm_measurement(
    *,
    label: str,
    measured_freq_hz: float | None,
    measured_duty_pct: float | None,
    expected_freq_hz: int,
    expected_duty_pct: float,
    freq_tol_pct: float,
    duty_tol_pct: float,
) -> bool:
    passed = True

    if measured_freq_hz is None:
        print(f"{label}: frequency invalid | FAIL")
        passed = False
    else:
        freq_err_pct = pct_error(measured_freq_hz, expected_freq_hz)
        freq_passed = freq_err_pct <= freq_tol_pct
        print(
            f"{label}: freq expected={expected_freq_hz:.2f} Hz | measured={measured_freq_hz:.2f} Hz | "
            f"error={freq_err_pct:.2f}% | {'PASS' if freq_passed else 'FAIL'}"
        )
        passed &= freq_passed

    if measured_duty_pct is None:
        print(f"{label}: duty invalid | FAIL")
        passed = False
    else:
        duty_err_pct = pct_error(measured_duty_pct, expected_duty_pct)
        duty_passed = duty_err_pct <= duty_tol_pct
        print(
            f"{label}: duty expected={expected_duty_pct:.2f}% | measured={measured_duty_pct:.2f}% | "
            f"error={duty_err_pct:.2f}% | {'PASS' if duty_passed else 'FAIL'}"
        )
        passed &= duty_passed

    return passed


def measure_burst_case(
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    *,
    channel: str,
    cut_pin: str,
    scope_channel: int,
    freq_hz: int,
    duty: float,
    burst_cmp: int,
    burst_per: int,
    averages: int,
    settle_seconds: float,
    trigger_edge_level: float,
    burst_timebase_scale: float | None,
    freq_tol_pct: float,
    duty_tol_pct: float,
    burst_period_tol_pct: float,
) -> bool:
    applied_freq_hz = resolve_pwm_frequency_hz(freq_hz)
    applied_duty = clamp_pwm_duty(duty)
    expected_duty_pct = applied_duty * 100.0
    carrier_period_s = 1.0 / applied_freq_hz
    expected_burst_period_nominal_s = burst_per / applied_freq_hz
    active_cycles = burst_per - burst_cmp
    expected_burst_on_s = active_cycles / applied_freq_hz
    expected_burst_off_s = burst_cmp / applied_freq_hz

    print("Setup")
    print(f"- Route: {cut_pin} -> {channel} -> Rigol CH{scope_channel}")
    print(
        f"- PWM: requested freq={freq_hz} Hz, applied freq={applied_freq_hz} Hz, "
        f"requested duty={duty * 100:.1f}%, applied duty={expected_duty_pct:.1f}%"
    )
    print(f"- Burst: bm_cmp={burst_cmp}, bm_per={burst_per}, active_cycles={active_cycles}")
    print(f"- Timing: on={expected_burst_on_s * 1e3:.2f} ms, off={expected_burst_off_s * 1e3:.2f} ms, period={expected_burst_period_nominal_s * 1e3:.2f} ms")

    mux.route(channel, cut_pin)
    cut.set_pwm(pin=cut_pin, freq_hz=freq_hz, duty=duty)
    print_route_diagnostics(cut, mux, channel, cut_pin)

    prepare_scope(
        scope,
        scope_channel,
        timebase_scale=default_carrier_timebase(applied_freq_hz, burst_per),
        trigger_edge_level=trigger_edge_level,
    )
    time.sleep(max(settle_seconds, 0.0))
    measured_freq_hz = average_valid_measurements(
        lambda: scope.read_frequency(scope_channel),
        averages=averages,
        settle_seconds=max(settle_seconds / 2.0, 0.1),
    )
    measured_duty_pct = average_valid_measurements(
        lambda: normalize_duty_percent(scope.read_duty_cycle(scope_channel)),
        averages=averages,
        settle_seconds=max(settle_seconds / 2.0, 0.1),
    )
    measured_period_s = average_valid_measurements(
        lambda: scope.read_period(scope_channel),
        averages=averages,
        settle_seconds=max(settle_seconds / 2.0, 0.1),
    )

    print("\nPre-burst checks")
    pre_burst_passed = check_pwm_measurement(
        label="normal PWM before burst",
        measured_freq_hz=measured_freq_hz,
        measured_duty_pct=measured_duty_pct,
        expected_freq_hz=applied_freq_hz,
        expected_duty_pct=expected_duty_pct,
        freq_tol_pct=freq_tol_pct,
        duty_tol_pct=duty_tol_pct,
    )
    if not pre_burst_passed:
        print("\nOverall: FAIL")
        return False

    cut.init_burst_mode(cut_pin)
    cut.configure_burst_mode(cut_pin, bm_cmp=burst_cmp, bm_per=burst_per)
    cut.start_burst_mode(cut_pin)

    prepare_scope(
        scope,
        scope_channel,
        timebase_scale=(
            burst_timebase_scale
            if burst_timebase_scale is not None
            else default_burst_timebase(applied_freq_hz, burst_per)
        ),
        trigger_edge_level=trigger_edge_level,
    )
    burst_settle_seconds = max(settle_seconds, expected_burst_period_nominal_s * 2.0, 0.1)
    time.sleep(burst_settle_seconds)
    scope.clear_measurements()
    scope.reset_statistics()
    time.sleep(max(settle_seconds, expected_burst_period_nominal_s * 10.0, 0.1))
    measured_burst_off_width_s = average_valid_measurements(
        lambda: scope.read_negative_width_max(scope_channel),
        averages=averages,
        settle_seconds=max(expected_burst_period_nominal_s * 2.0, settle_seconds / 2.0, 0.1),
    )

    measured_duty_fraction = (
        measured_duty_pct / 100.0
        if measured_duty_pct is not None
        else applied_duty
    )
    measured_carrier_period_s = (
        measured_period_s
        if measured_period_s is not None
        else carrier_period_s
    )
    calculated_active_pulses_s = calculate_burst_active_pulses_time_s(
        pulse_count=active_cycles,
        duty=measured_duty_fraction,
        period_s=measured_carrier_period_s,
    )
    expected_off_width_s = expected_burst_period_nominal_s - calculated_active_pulses_s
    measured_burst_period_s = (
        calculated_active_pulses_s + measured_burst_off_width_s
        if measured_burst_off_width_s is not None
        else None
    )

    passed = True
    print("\nBurst checks")

    if measured_burst_period_s is None:
        print(
            "burst timing: "
            f"active pulses calculated={calculated_active_pulses_s * 1e3:.2f} ms | "
            f"expected -Width={expected_off_width_s * 1e3:.2f} ms | "
            "scope MAX -Width invalid | FAIL"
        )
        passed = False
    else:
        burst_err_pct = pct_error(measured_burst_period_s, expected_burst_period_nominal_s)
        off_width_err_pct = pct_error(measured_burst_off_width_s, expected_off_width_s)
        burst_period_passed = burst_err_pct <= burst_period_tol_pct
        burst_active_passed = measured_burst_period_s > carrier_period_s
        burst_passed = burst_period_passed and burst_active_passed
        print(
            "burst timing: "
            f"n={active_cycles}, D={measured_duty_fraction:.4f}, T={measured_carrier_period_s * 1e3:.4f} ms | "
            f"active pulses={calculated_active_pulses_s * 1e3:.2f} ms | "
            f"expected -Width={expected_off_width_s * 1e3:.2f} ms | "
            f"scope MAX -Width={measured_burst_off_width_s * 1e3:.2f} ms | "
            f"-Width error={off_width_err_pct:.2f}%"
        )
        print(
            f"burst: nominal period={expected_burst_period_nominal_s * 1e3:.2f} ms | "
            f"measured={measured_burst_period_s * 1e3:.2f} ms | "
            f"carrier period={carrier_period_s * 1e3:.2f} ms | "
            f"error={burst_err_pct:.2f}% | tolerance={burst_period_tol_pct:.2f}% | "
            f"{'PASS' if burst_period_passed and burst_active_passed else 'FAIL'}"
        )
        passed &= burst_passed

    print(f"\nOverall: {'PASS' if passed else 'FAIL'}")
    return passed


def main() -> int:
    args = build_parser().parse_args()

    try:
        channel = normalize_channel(args.channel)
        run_all_pwm_pins = parse_bool_flag(args.all_pwm_pins)
        resolve_pwm_frequency_hz(args.freq)
        if args.burst_cmp < 0:
            raise ValueError("burst-off-cycles / bm-cmp must be >= 0")
        if args.burst_per <= 0:
            raise ValueError("burst-total-cycles / bm-per must be > 0")
        if args.burst_cmp >= args.burst_per:
            raise ValueError("burst-off-cycles / bm-cmp must be smaller than burst-total-cycles / bm-per")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if run_all_pwm_pins:
            pin_plan = list(ALL_PWM_PINS)
        else:
            pin_plan = [normalize_cut_pin(args.cut_pin)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    scope_channel = CHANNEL_TO_SCOPE[channel]
    scope = Oscilloscope()
    mux = MuxController(port=args.mux_port)
    cut = CutPwmController(port=args.cut_port)

    pass_count = 0
    fail_count = 0

    try:
        scope.connect()
        mux.connect()
        cut.connect()

        for index, cut_pin in enumerate(pin_plan, start=1):
            print(f"\n=== Burst-mode case {index}/{len(pin_plan)}: {cut_pin} ===")
            case_passed = False
            try:
                case_passed = measure_burst_case(
                    scope,
                    mux,
                    cut,
                    channel=channel,
                    cut_pin=cut_pin,
                    scope_channel=scope_channel,
                    freq_hz=args.freq,
                    duty=args.duty,
                    burst_cmp=args.burst_cmp,
                    burst_per=args.burst_per,
                    averages=args.averages,
                    settle_seconds=args.settle_seconds,
                    trigger_edge_level=args.trigger_edge_level,
                    burst_timebase_scale=args.burst_timebase_scale,
                    freq_tol_pct=args.freq_tol_pct,
                    duty_tol_pct=args.duty_tol_pct,
                    burst_period_tol_pct=args.burst_period_tol_pct,
                )
            finally:
                try:
                    cut.stop_burst_mode(cut_pin)
                except Exception:
                    pass
                try:
                    cut.deinit_burst_mode(cut_pin)
                except Exception:
                    pass
                try:
                    cut.disable(pin=cut_pin, freq_hz=args.freq, duty=args.duty)
                except Exception:
                    pass
                try:
                    mux.disable_route(channel, cut_pin)
                except Exception:
                    pass
                print("Cleanup: outputs returned to idle state.")

            if case_passed:
                pass_count += 1
            else:
                fail_count += 1

        print(f"\nSummary: {pass_count} burst PASS, {fail_count} burst FAIL, total {len(pin_plan)}")
        return 0 if fail_count == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
