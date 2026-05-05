#!/usr/bin/env python3
"""Automatic dead-time check for a complementary PWM pair."""

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
CHANNEL_TO_SCOPE = {"ch1": 1, "ch2": 2, "ch3": 3, "ch4": 4}
COMPLEMENTARY_PWM_PAIRS = (
    ("PA8", "PA9"),
    ("PB12", "PB13"),
    ("PB14", "PB15"),
    ("PC8", "PC9"),
    ("PC6", "PC7"),
)
DELAY_ITEMS = ("RFDelay", "FRDelay")
SCOPE_CHANNEL_PROBE = 9.8
SCOPE_CHANNEL_SCALE = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route two CUT PWM signals through MUX and check dead time automatically"
    )
    parser.add_argument("--cut-port", required=True, help="CUT serial port, for example COM22")
    parser.add_argument("--mux-port", required=True, help="MUX serial port, for example COM20")
    parser.add_argument("--cut-pin-a", default="PA8", help="First PWM-capable CUT pin name, for example PA8")
    parser.add_argument("--cut-pin-b", default="PA9", help="Second PWM-capable CUT pin name, for example PA9")
    parser.add_argument(
        "--all-complementary-pairs",
        default="false",
        help="When true, run the dead-time test on every built-in complementary PWM pair",
    )
    parser.add_argument("--channel-a", default="ch1", help="First shield channel name, for example ch1")
    parser.add_argument("--channel-b", default="ch2", help="Second shield channel name, for example ch2")
    parser.add_argument("--freq", type=int, default=1000, help="PWM frequency in Hz")
    parser.add_argument("--duty", type=float, default=0.50, help="Duty value as a fraction")
    parser.add_argument("--dead-time-pct", type=float, default=1.0, help="Total dead time as percent of PWM period. Default = 1.0")
    parser.add_argument("--dead-rise-ns", type=int, help="Configured wDeadRise value in ns")
    parser.add_argument("--dead-fall-ns", type=int, help="Configured wDeadFall value in ns")
    parser.add_argument("--expected-rfdelay-ns", type=float, help="Expected RFDelay magnitude in ns. Default = dead-fall-ns")
    parser.add_argument("--expected-frdelay-ns", type=float, help="Expected FRDelay magnitude in ns. Default = dead-rise-ns")
    parser.add_argument("--delay-tol-pct", type=float, default=20.0, help="Allowed dead-time error as percent of expected delay. Default = 20.0")
    parser.add_argument("--settle-seconds", type=float, default=0.3, help="Delay before reading the oscilloscope")
    parser.add_argument("--averages", type=int, default=3, help="Number of delay measurements to average")
    parser.add_argument("--trigger-edge-level", type=float, default=1.5, help="Rigol TRIGger:EDGE:LEVel in volts")
    parser.add_argument("--timebase-scale", type=float, help="Rigol timebase scale in seconds/div")
    return parser


def default_timebase_for_dead_time(freq_hz: int, expected_delay_ns: float) -> float:
    period_scale = max(1.0 / max(freq_hz * 50.0, 1.0), 1e-7)
    event_scale = max(expected_delay_ns * 20.0 * 1e-9, 1e-7)
    return min(period_scale, event_scale)


def resolve_dead_times_ns(freq_hz: int, dead_time_pct: float, dead_rise_ns: int | None, dead_fall_ns: int | None) -> tuple[int, int]:
    if dead_rise_ns is not None and dead_fall_ns is not None:
        return dead_rise_ns, dead_fall_ns
    if freq_hz <= 0:
        raise ValueError("freq must be > 0")
    if dead_time_pct < 0:
        raise ValueError("dead-time-pct must be >= 0")

    period_ns = 1e9 / float(freq_hz)
    total_dead_time_ns = period_ns * (dead_time_pct / 100.0)
    rise_ns = int(round(total_dead_time_ns / 2.0))
    fall_ns = int(round(total_dead_time_ns / 2.0))

    if dead_rise_ns is not None:
        rise_ns = dead_rise_ns
    if dead_fall_ns is not None:
        fall_ns = dead_fall_ns
    return rise_ns, fall_ns


def resolve_delay_tolerance_ns(expected_delay_ns: float, delay_tol_pct: float) -> float:
    if delay_tol_pct < 0:
        raise ValueError("delay-tol-pct must be >= 0")
    return float(expected_delay_ns) * (delay_tol_pct / 100.0)


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


def print_route_diagnostics(cut: CutPwmController, mux: MuxController, *, channel_a: str, cut_pin_a: str, channel_b: str, cut_pin_b: str) -> None:
    cut_state = cut.read_state()
    route_a = mux.read_route(channel=channel_a, cut_pin=cut_pin_a)
    route_b = mux.read_route(channel=channel_b, cut_pin=cut_pin_b)
    source_a = PWM_SOURCES[cut_pin_a]
    source_b = PWM_SOURCES[cut_pin_b]
    print(
        f"route A {cut_pin_a} -> CUT(PWM{source_a['unit']}{source_a['output']}) via {route_a.mux}/{route_a.channel}"
    )
    print(
        f"route B {cut_pin_b} -> CUT(PWM{source_b['unit']}{source_b['output']}) via {route_b.mux}/{route_b.channel}"
    )
    print(f"readback CUT: enabled={cut_state.enabled} outputs={list(cut_state.enabled_outputs)} status={cut_state.status}")
    print(f"readback MUX A: channel={route_a.channel} mux={route_a.mux} input={route_a.input_index} enable={route_a.enabled}")
    print(f"readback MUX B: channel={route_b.channel} mux={route_b.mux} input={route_b.input_index} enable={route_b.enabled}")


def prepare_scope(
    scope: Oscilloscope,
    scope_channel_a: int,
    scope_channel_b: int,
    freq_hz: int,
    expected_delay_ns: float,
    trigger_channel: int,
    trigger_slope: str,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> None:
    chosen_timebase_scale = (
        timebase_scale
        if timebase_scale is not None
        else default_timebase_for_dead_time(freq_hz, expected_delay_ns)
    )
    for scope_channel in (scope_channel_a, scope_channel_b):
        scope.configure_calibrated_channel(
            scope_channel,
            channel_probe=SCOPE_CHANNEL_PROBE,
            channel_scale=SCOPE_CHANNEL_SCALE,
            offset_volts=0.0,
            coupling="DC",
            enabled=True,
        )
    scope.set_timebase(chosen_timebase_scale)
    scope.configure_edge_trigger(channel=trigger_channel, trigger_edge_level=trigger_edge_level, slope=trigger_slope)
    scope.run()


def try_average_delay(scope: Oscilloscope, delay_item: str, scope_channel_a: int, scope_channel_b: int, averages: int, settle_seconds: float) -> float | None:
    values: list[float] = []
    for _ in range(max(1, averages)):
        time.sleep(max(0.0, settle_seconds))
        try:
            value_s = scope.read_delay(delay_item, scope_channel_a, scope_channel_b)
            values.append(value_s * 1e9)
        except Exception:
            continue
    if not values:
        return None
    return sum(values) / len(values)


def evaluate_delay(measured_ns: float | None, expected_ns: float, tolerance_ns: float) -> tuple[float | None, bool]:
    if measured_ns is None:
        return None, False
    error_ns = abs(abs(measured_ns) - expected_ns)
    return error_ns, error_ns <= tolerance_ns


def measure_delay_item(scope: Oscilloscope, *, item: str, scope_channel_a: int, scope_channel_b: int, freq_hz: int, expected_delay_ns: float, trigger_edge_level: float, timebase_scale: float | None, averages: int, settle_seconds: float) -> float | None:
    trigger_slope = "POS" if item == "RFDelay" else "NEG"
    prepare_scope(
        scope,
        scope_channel_a,
        scope_channel_b,
        freq_hz,
        expected_delay_ns,
        scope_channel_a,
        trigger_slope,
        trigger_edge_level,
        timebase_scale,
    )
    time.sleep(max(settle_seconds, 0.0))
    return try_average_delay(
        scope,
        item,
        scope_channel_a,
        scope_channel_b,
        averages,
        max(settle_seconds / 2.0, 0.05),
    )


def measure_delay_item_with_retry(
    scope: Oscilloscope,
    *,
    item: str,
    scope_channel_a: int,
    scope_channel_b: int,
    freq_hz: int,
    expected_delay_ns: float,
    trigger_edge_level: float,
    timebase_scale: float | None,
    averages: int,
    settle_seconds: float,
) -> float | None:
    measured_ns = measure_delay_item(
        scope,
        item=item,
        scope_channel_a=scope_channel_a,
        scope_channel_b=scope_channel_b,
        freq_hz=freq_hz,
        expected_delay_ns=expected_delay_ns,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
        averages=averages,
        settle_seconds=settle_seconds,
    )
    if measured_ns is not None:
        return measured_ns

    print(f"{item}: retrying with swapped scope-channel order")
    measured_ns = measure_delay_item(
        scope,
        item=item,
        scope_channel_a=scope_channel_b,
        scope_channel_b=scope_channel_a,
        freq_hz=freq_hz,
        expected_delay_ns=expected_delay_ns,
        trigger_edge_level=trigger_edge_level,
        timebase_scale=timebase_scale,
        averages=averages,
        settle_seconds=settle_seconds,
    )
    if measured_ns is not None:
        print(f"{item}: recovered with swapped scope-channel order")
    return measured_ns


def print_delay_result(name: str, measured_ns: float | None, expected_ns: float, tolerance_ns: float) -> bool:
    error_ns, passed = evaluate_delay(measured_ns, expected_ns, tolerance_ns)
    measured_text = "invalid" if measured_ns is None else f"{abs(measured_ns):.2f} ns"
    error_text = "invalid" if error_ns is None else f"{error_ns:.2f} ns"
    print(
        f"{name}: expected={expected_ns:.2f} ns | measured={measured_text} | "
        f"tolerance={tolerance_ns:.2f} ns | error={error_text} | result={'PASS' if passed else 'FAIL'}"
    )
    return passed


def run_dead_time_case(
    *,
    scope: Oscilloscope,
    mux: MuxController,
    cut: CutPwmController,
    cut_pin_a: str,
    cut_pin_b: str,
    channel_a: str,
    channel_b: str,
    freq_hz: int,
    duty: float,
    dead_rise_ns: int,
    dead_fall_ns: int,
    expected_rfdelay_ns: float,
    expected_frdelay_ns: float,
    delay_tol_rf_ns: float,
    delay_tol_fr_ns: float,
    settle_seconds: float,
    averages: int,
    trigger_edge_level: float,
    timebase_scale: float | None,
) -> bool:
    reference_delay_ns = max(expected_rfdelay_ns, expected_frdelay_ns)
    scope_channel_a = CHANNEL_TO_SCOPE[channel_a]
    scope_channel_b = CHANNEL_TO_SCOPE[channel_b]

    cut.disable_pair(
        pin_a=cut_pin_a,
        pin_b=cut_pin_b,
        freq_hz=freq_hz,
        duty=duty,
        dead_rise_ns=dead_rise_ns,
        dead_fall_ns=dead_fall_ns,
    )
    time.sleep(max(settle_seconds, 0.0))

    mux.route_pair(channel_a, cut_pin_a, channel_b, cut_pin_b)
    print("Configuration")
    print(f"- PWM A: {cut_pin_a} -> {channel_a} -> Rigol CH{scope_channel_a}")
    print(f"- PWM B: {cut_pin_b} -> {channel_b} -> Rigol CH{scope_channel_b}")
    print(f"- Requested frequency: {freq_hz} Hz")
    print(f"- Applied frequency: {resolve_pwm_frequency_hz(freq_hz)} Hz")
    print(f"- Requested duty: {duty * 100:.1f}%")
    print(f"- Applied duty: {clamp_pwm_duty(duty) * 100:.1f}%")
    print(f"- Dead rise: {dead_rise_ns} ns")
    print(f"- Dead fall: {dead_fall_ns} ns")
    command = cut.set_pwm_pair(
        pin_a=cut_pin_a,
        pin_b=cut_pin_b,
        freq_hz=freq_hz,
        duty=duty,
        dead_rise_ns=dead_rise_ns,
        dead_fall_ns=dead_fall_ns,
    )
    time.sleep(max(settle_seconds, 0.0))
    print_route_diagnostics(
        cut,
        mux,
        channel_a=channel_a,
        cut_pin_a=cut_pin_a,
        channel_b=channel_b,
        cut_pin_b=cut_pin_b,
    )

    measurements_ns: dict[str, float | None] = {}
    for item in DELAY_ITEMS:
        measurements_ns[item] = measure_delay_item_with_retry(
            scope,
            item=item,
            scope_channel_a=scope_channel_a,
            scope_channel_b=scope_channel_b,
            freq_hz=command.freq_hz,
            expected_delay_ns=reference_delay_ns,
            trigger_edge_level=trigger_edge_level,
            timebase_scale=timebase_scale,
            averages=averages,
            settle_seconds=settle_seconds,
        )

    rfdelay_ns = measurements_ns["RFDelay"]
    frdelay_ns = measurements_ns["FRDelay"]
    if rfdelay_ns is None and frdelay_ns is None:
        print("dead-time measurement failed: the oscilloscope could not extract a valid RFDelay or FRDelay")
        return False

    print("\nMeasurements")
    rf_pass = print_delay_result("RFDelay", rfdelay_ns, expected_rfdelay_ns, delay_tol_rf_ns)
    fr_pass = print_delay_result("FRDelay", frdelay_ns, expected_frdelay_ns, delay_tol_fr_ns)
    passed = rf_pass and fr_pass

    print(f"\nOverall: {'PASS' if passed else 'FAIL'}")
    return passed


def main() -> int:
    args = build_parser().parse_args()

    try:
        channel_a = normalize_channel(args.channel_a)
        channel_b = normalize_channel(args.channel_b)
        run_all_complementary_pairs = parse_bool_flag(args.all_complementary_pairs)
        applied_freq_hz = resolve_pwm_frequency_hz(args.freq)
        dead_rise_ns, dead_fall_ns = resolve_dead_times_ns(
            applied_freq_hz,
            args.dead_time_pct,
            args.dead_rise_ns,
            args.dead_fall_ns,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if run_all_complementary_pairs:
            pair_plan = list(COMPLEMENTARY_PWM_PAIRS)
        else:
            pair_plan = [
                (
                    normalize_cut_pin(args.cut_pin_a),
                    normalize_cut_pin(args.cut_pin_b),
                )
            ]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    expected_rfdelay_ns = (
        float(args.expected_rfdelay_ns) if args.expected_rfdelay_ns is not None else float(dead_fall_ns)
    )
    expected_frdelay_ns = (
        float(args.expected_frdelay_ns) if args.expected_frdelay_ns is not None else float(dead_rise_ns)
    )
    delay_tol_rf_ns = resolve_delay_tolerance_ns(expected_rfdelay_ns, args.delay_tol_pct)
    delay_tol_fr_ns = resolve_delay_tolerance_ns(expected_frdelay_ns, args.delay_tol_pct)
    scope = Oscilloscope()
    mux = MuxController(port=args.mux_port)
    cut = CutPwmController(port=args.cut_port)

    try:
        scope.connect()
        mux.connect()
        cut.connect()
        passed_pairs = 0
        failed_pairs = 0

        for index, (cut_pin_a, cut_pin_b) in enumerate(pair_plan, start=1):
            print(f"\n=== Dead-time case {index}/{len(pair_plan)}: {cut_pin_a} / {cut_pin_b} ===")
            pair_passed = False
            try:
                pair_passed = run_dead_time_case(
                    scope=scope,
                    mux=mux,
                    cut=cut,
                    cut_pin_a=cut_pin_a,
                    cut_pin_b=cut_pin_b,
                    channel_a=channel_a,
                    channel_b=channel_b,
                    freq_hz=args.freq,
                    duty=args.duty,
                    dead_rise_ns=dead_rise_ns,
                    dead_fall_ns=dead_fall_ns,
                    expected_rfdelay_ns=expected_rfdelay_ns,
                    expected_frdelay_ns=expected_frdelay_ns,
                    delay_tol_rf_ns=delay_tol_rf_ns,
                    delay_tol_fr_ns=delay_tol_fr_ns,
                    settle_seconds=args.settle_seconds,
                    averages=args.averages,
                    trigger_edge_level=args.trigger_edge_level,
                    timebase_scale=args.timebase_scale,
                )
            except Exception as exc:
                print(f"\nOverall: FAIL")
                print(f"dead-time test raised an exception for {cut_pin_a}/{cut_pin_b}: {exc}")
            finally:
                try:
                    cut.disable_pair(
                        pin_a=cut_pin_a,
                        pin_b=cut_pin_b,
                        freq_hz=args.freq,
                        duty=args.duty,
                        dead_rise_ns=dead_rise_ns,
                        dead_fall_ns=dead_fall_ns,
                    )
                except Exception:
                    pass
                try:
                    mux.disable_route(channel_a, cut_pin_a)
                except Exception:
                    pass
                try:
                    mux.disable_route(channel_b, cut_pin_b)
                except Exception:
                    pass
                print("Cleanup: outputs returned to idle state.")

            if pair_passed:
                passed_pairs += 1
            else:
                failed_pairs += 1

        print(f"\nSummary: {passed_pairs} dead-time PASS, {failed_pairs} dead-time FAIL, total {len(pair_plan)}")
        return 0 if failed_pairs == 0 else 1
    finally:
        cut.close()
        mux.close()
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
