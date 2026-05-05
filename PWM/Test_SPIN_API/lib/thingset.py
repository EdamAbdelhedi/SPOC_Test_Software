#!/usr/bin/env python3
"""Minimal ThingSet text-mode helper used by the hardware_tests package."""

from __future__ import annotations

import json
import re
import time
from typing import Optional, Tuple

import serial  # type: ignore


PROMPT_RE = re.compile(rb"([A-Za-z0-9_-]+):~\$ ")


# Parse a scalar string into a JSON-compatible Python value for ThingSet updates.
def parse_scalar(value: str) -> object:
    text = value.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        if text.startswith("0x"):
            return int(text, 16)
        return int(text)
    except Exception:
        pass
    try:
        return float(text)
    except Exception:
        pass
    return text


# Small serial shell wrapper that can send ThingSet GET/UPDATE/EXEC commands.
class ThingSetShell:
    def __init__(self, ser: serial.Serial, verbose: bool = False, cmd_prefix: str = ""):
        self.ser = ser
        self.verbose = verbose
        self.cmd_prefix = cmd_prefix
        self.prompt: Optional[bytes] = None
        try:
            self.ser.write_timeout = 0.5
        except Exception:
            pass

    # Print serial debug traces when verbose mode is enabled.
    def _dbg(self, message: str) -> None:
        if self.verbose:
            print(f"[DBG] {message}")

    # Read from the serial port until the Zephyr shell prompt is seen or a timeout expires.
    def read_until_prompt(self, timeout: float = 2.0) -> bytes:
        self.ser.timeout = 0.05
        data = bytearray()
        start = time.time()
        last_rx = start
        while True:
            chunk = self.ser.read(4096)
            now = time.time()
            if chunk:
                data.extend(chunk)
                last_rx = now
                match = PROMPT_RE.search(data)
                if match:
                    self.prompt = match.group(0)
                    break
            if now - last_rx > 0.2 or now - start > timeout:
                break
        return bytes(data)

    # Send one shell command and capture its response text.
    def send_cmd(self, cmd: str, read_timeout: float = 2.0) -> bytes:
        if not (cmd.endswith("\n") or cmd.endswith("\r")):
            cmd += "\r\n"
        full_cmd = self.cmd_prefix + cmd
        self._dbg(f"TX: {full_cmd.strip()}")
        self.ser.write(full_cmd.encode())
        self.ser.flush()
        response = self.read_until_prompt(timeout=read_timeout)
        self._dbg(f"RX: {response[-200:].decode(errors='ignore')}")
        return response

    # Enter the ThingSet shell context so later GET/UPDATE commands work.
    def enter_thingset(self) -> bool:
        try:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            for _ in range(3):
                try:
                    self.ser.write(b"\r\n")
                    self.ser.flush()
                except Exception:
                    pass
                self.read_until_prompt(timeout=0.5)
                if self.prompt is not None:
                    break
        except Exception:
            pass

        try:
            self.send_cmd("select thingset", read_timeout=0.5)
        except Exception:
            pass

        def try_probe() -> bool:
            reply = self.send_cmd("?", read_timeout=1.0)
            return self._extract_json(reply.decode(errors="ignore")) is not None

        if try_probe():
            return True

        old_prefix = self.cmd_prefix
        self.cmd_prefix = "thingset "
        self._dbg("Switching to 'thingset ' command prefix")
        ok = try_probe()
        if not ok:
            self.cmd_prefix = old_prefix
        return ok

    # Extract the first JSON object or array from a shell response.
    def _extract_json(self, text: str):
        start = None
        for index, char in enumerate(text):
            if char in "[{":
                start = index
                break
        if start is None:
            return None
        stack: list[str] = []
        end = None
        for index in range(start, len(text)):
            char = text[index]
            if char in "[{":
                stack.append(char)
            elif char in "]}":
                if not stack:
                    return None
                top = stack.pop()
                if (top == "[" and char != "]") or (top == "{" and char != "}"):
                    return None
                if not stack:
                    end = index + 1
                    break
        if end is None:
            return None
        try:
            return json.loads(text[start:end])
        except Exception:
            return None

    # Read one ThingSet path and return a parsed scalar or JSON fragment as text.
    def get_value(self, path: str, timeout: float = 1.5) -> Tuple[bool, Optional[str]]:
        arg = path[1:] if path.startswith("/") else path
        response = self.send_cmd(f"?{arg}", read_timeout=timeout).decode(errors="ignore")
        json_value = self._extract_json(response)
        if json_value is not None:
            if isinstance(json_value, dict) and json_value:
                try:
                    value = next(iter(json_value.values()))
                except Exception:
                    value = json_value
            else:
                value = json_value
            return True, str(value)
        match = re.search(r":[0-9A-Fa-f]{2,}\s+(.*)", response)
        if match:
            token = match.group(1).strip().splitlines()[0].strip()
            return True, token if token else None
        return False, None

    # Write one ThingSet value and return success based on the status code in the reply.
    def set_value(self, path: str, value: str, timeout: float = 2.0) -> Tuple[bool, str]:
        normalized = path[1:] if path.startswith("/") else path
        if "/" in normalized:
            parent, leaf = normalized.rsplit("/", 1)
        else:
            parent, leaf = "", normalized
        payload = json.dumps({leaf: parse_scalar(value)}, separators=(",", ":")).replace('"', '\\"')
        command = f"={parent or ''} {payload}".strip()
        response = self.send_cmd(command, read_timeout=timeout).decode(errors="ignore")
        ok = bool(re.search(r":(84|85)(\s|$)", response)) and ("A3" not in response)
        return ok, response

    # Execute one ThingSet x* node.
    def exec_node(self, path: str, timeout: float = 2.0) -> Tuple[bool, str]:
        arg = path[1:] if path.startswith("/") else path
        response = self.send_cmd(f"!{arg}", read_timeout=timeout).decode(errors="ignore")
        ok = bool(re.search(r":(84|85)(\s|$)", response)) and ("A3" not in response)
        return ok, response
