from __future__ import annotations

import json
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from platformio.public import TestCase, TestStatus


BENCH_REQUIREMENTS = {
    "pyserial": "serial",
    "pyvisa": "pyvisa",
    "pyvisa-py": "pyvisa_py",
}


def _project_root_from_config(config_path: Path) -> Path:
    return config_path.parents[3]


def _split_command(command: str) -> list[str]:
    return shlex.split(command, posix=(os.name != "nt"))


def _format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _command_exists(command: list[str]) -> bool:
    executable = command[0]
    if Path(executable).is_file():
        return True
    return shutil.which(executable) is not None


def _probe_python_command(command: list[str]) -> bool:
    if not _command_exists(command):
        return False
    probe = subprocess.run(
        [*command, "-c", "import sys; print(sys.executable)"],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def _default_python_candidates() -> list[list[str]]:
    candidates: list[list[str]] = []
    if sys.executable:
        candidates.append([sys.executable])
    if os.name == "nt":
        candidates.extend((["py", "-3"], ["py"], ["python"]))
    else:
        candidates.extend((["python3"], ["python"]))
    return candidates


def _resolve_python_command(config: dict) -> list[str]:
    configured = os.environ.get("BENCH_PYTHON") or config.get("python")
    candidates = [_split_command(configured)] if configured else _default_python_candidates()

    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        key = tuple(candidate)
        if key in seen or not candidate:
            continue
        seen.add(key)
        if _probe_python_command(candidate):
            return candidate

    if configured:
        raise RuntimeError(
            "Could not resolve bench Python command "
            f"`{configured}` on this {sys.platform} host."
        )

    raise RuntimeError(
        "Could not find a usable Python interpreter for the bench runner. "
        "Set BENCH_PYTHON to an explicit executable if needed."
    )


def _resolve_command(config: dict, project_root: Path) -> list[str]:
    python_cmd = _resolve_python_command(config)
    script_path = (project_root / config["script"]).resolve()
    return [*python_cmd, str(script_path)]


def _missing_requirements_for_current_python() -> list[str]:
    missing: list[str] = []
    for package_name, module_name in BENCH_REQUIREMENTS.items():
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)
    return missing


def _missing_requirements_for_python(python_cmd: list[str]) -> list[str]:
    probe = subprocess.run(
        [
            *python_cmd,
            "-c",
            (
                "import importlib.util, json; "
                f"checks={json.dumps(BENCH_REQUIREMENTS)}; "
                "missing=[pkg for pkg, mod in checks.items() if importlib.util.find_spec(mod) is None]; "
                "print(json.dumps(missing))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return list(BENCH_REQUIREMENTS)
    try:
        return json.loads(probe.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return list(BENCH_REQUIREMENTS)


def _install_requirements(python_cmd: list[str], packages: list[str]) -> None:
    if not packages:
        return

    sys.stdout.write(
        "Installing bench Python dependencies with "
        f"{_format_command(python_cmd)}: {', '.join(packages)}\n"
    )
    pip_install = subprocess.run(
        [*python_cmd, "-m", "pip", "install", *packages],
        check=False,
    )
    if pip_install.returncode == 0:
        return

    sys.stdout.write(
        f"`{_format_command([*python_cmd, '-m', 'pip'])}` is unavailable; "
        f"trying `{_format_command([*python_cmd, '-m', 'ensurepip', '--upgrade'])}`.\n"
    )
    ensure_pip = subprocess.run(
        [*python_cmd, "-m", "ensurepip", "--upgrade"],
        check=False,
    )
    if ensure_pip.returncode != 0:
        raise RuntimeError(
            "Could not bootstrap pip in the bench Python environment. "
            "Install pip for that interpreter or set BENCH_PYTHON to a Python with pip available."
        )

    subprocess.run(
        [*python_cmd, "-m", "pip", "install", *packages],
        check=True,
    )


def _ensure_bench_dependencies(python_cmd: list[str]) -> None:
    if python_cmd == [sys.executable]:
        missing = _missing_requirements_for_current_python()
    else:
        missing = _missing_requirements_for_python(python_cmd)

    if not missing:
        return

    _install_requirements(python_cmd, missing)


def _resolve_args(config: dict) -> list[str]:
    args: list[str] = []
    missing: list[str] = []

    for item in config.get("args", []):
        flag = item["flag"]
        env_name = item.get("env")
        required = bool(item.get("required", False))
        default = item.get("default")

        value = os.environ.get(env_name) if env_name else None
        if value in (None, ""):
            value = default

        if value in (None, ""):
            if required:
                missing.append(env_name or flag)
            continue

        if item.get("multiple") and isinstance(value, str):
            parsed_values = shlex.split(value)
            args.append(flag)
            args.extend(parsed_values)
        elif isinstance(value, list):
            args.append(flag)
            args.extend(str(entry) for entry in value)
        else:
            args.extend([flag, str(value)])

    if missing:
        raise RuntimeError(f"Missing required bench environment variable(s): {', '.join(missing)}")

    return args


def run_script_suite(runner, config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    project_root = _project_root_from_config(config_path)
    suite_name = config["name"]

    command = _resolve_command(config, project_root)
    python_cmd = command[:-1]

    started = time.perf_counter()
    output_lines: list[str] = []

    try:
        _ensure_bench_dependencies(python_cmd)
        command.extend(_resolve_args(config))
        process = subprocess.Popen(
            command,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        runner.test_suite.add_case(
            TestCase(
                name=suite_name,
                status=TestStatus.ERRORED,
                message=f"Could not start bench command: {exc}",
                exception=exc,
            )
        )
        return

    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        output_lines.append(line)

    return_code = process.wait()
    duration = time.perf_counter() - started
    combined_output = "".join(output_lines)

    if return_code == 0:
        runner.test_suite.add_case(
            TestCase(
                name=suite_name,
                status=TestStatus.PASSED,
                duration=duration,
                stdout=combined_output,
            )
        )
        return

    runner.test_suite.add_case(
        TestCase(
            name=suite_name,
            status=TestStatus.FAILED,
            message=f"Bench script exited with code {return_code}",
            duration=duration,
            stdout=combined_output,
        )
    )
