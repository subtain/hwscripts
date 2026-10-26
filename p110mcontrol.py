#!/usr/bin/env python3
"""
Persistent loopback-only HTTP bridge for a Matter plug via chip-tool.

Endpoints:
    POST /plug/on
    POST /plug/off
    GET /plug/status

POST /plug/status is also accepted for compatibility/testing.

Security model:
- Run this process as root via sudo.
- HTTP binds only to 127.0.0.1.
- Use an SSH local tunnel for remote access.
- No HTTP token is used.
- Node ID and endpoint ID are startup-only arguments.
- This script never deletes Matter KVS credentials or /tmp/chip_* files.

Generic launch:

sudo /path/to/venv/bin/python /path/to/p110mcontrol.py \
  --node-id 1 \
  --endpoint-id 1 \
  --listen-host 127.0.0.1 \
  --listen-port 8080
"""

import argparse
import atexit
import os
import re
import threading
import time
from datetime import UTC, datetime

import pexpect
from flask import Flask, jsonify, request
from waitress import serve


# =================================================================
# chip-tool configuration
# =================================================================

CHIP_TOOL = "/snap/bin/chip-tool"

# IMPORTANT:
# Match only the real chip-tool prompt at the beginning of a line.
#
# Matter debug logs can contain strings such as:
# [EM] >>> [E:...]
#
# A plain r">>>" incorrectly matches those logs. This anchored expression
# matches only:
# >>>
# at the beginning of a terminal line.
PROMPT = r"(?m)^>>>[ \t]*"

START_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 30

# Retained only for failure responses; successful HTTP responses are concise.
MAX_OUTPUT_CHARS = 20000


# =================================================================
# Arguments and utilities
# =================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Persistent local HTTP bridge for Matter chip-tool."
    )

    parser.add_argument(
        "--node-id",
        required=True,
        type=int,
        help="Matter destination node ID, for example: 1",
    )

    parser.add_argument(
        "--endpoint-id",
        required=True,
        type=int,
        help="Matter endpoint ID, for example: 1",
    )

    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="Must be 127.0.0.1. Default: 127.0.0.1",
    )

    parser.add_argument(
        "--listen-port",
        default=8080,
        type=int,
        help="HTTP listen port. Default: 8080",
    )

    args = parser.parse_args()

    # Prevent accidental exposure of the unauthenticated local API.
    if args.listen_host != "127.0.0.1":
        parser.error(
            "--listen-host must be 127.0.0.1. "
            "Use an SSH local tunnel for remote access."
        )

    if not 1 <= args.listen_port <= 65535:
        parser.error("--listen-port must be between 1 and 65535")

    if args.node_id < 0:
        parser.error("--node-id must be non-negative")

    if args.endpoint_id < 0:
        parser.error("--endpoint-id must be non-negative")

    return args


ARGS = parse_arguments()


def utc_timestamp():
    """Return an ISO-8601 timestamp in UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def log(message):
    """Print a timestamped diagnostic line in the server terminal."""
    print(f"[{utc_timestamp()}] {message}", flush=True)


def clean_output(value):
    """Clean terminal control characters and bound diagnostic output size."""
    if value is None:
        return ""

    text = str(value).replace("\r", "")

    # Remove ANSI terminal escape sequences if present.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = text.strip()

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[-MAX_OUTPUT_CHARS:] + "\n[output truncated]"

    return text


# =================================================================
# Persistent chip-tool process controller
# =================================================================

class ChipToolError(RuntimeError):
    """A chip-tool startup or Matter command failure."""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output


class PersistentChipTool:
    """
    Keep one root-owned `chip-tool interactive start` child process alive.

    All requests are serialized because one interactive chip-tool process
    can process only one command at a time.

    On a failure:
      1. Stop the current child process.
      2. Start a replacement child.
      3. Retry the requested command exactly once.

    This script does NOT delete or reset Matter controller state, KVS files,
    credentials, or /tmp/chip_* files.
    """

    def __init__(self, node_id, endpoint_id):
        self.node_id = node_id
        self.endpoint_id = endpoint_id
        self.process = None
        self.lock = threading.Lock()

    def is_alive(self):
        """Return whether the interactive chip-tool child is alive."""
        return self.process is not None and self.process.isalive()

    def start(self):
        """Start chip-tool interactive mode and wait for the real prompt."""
        if self.is_alive():
            return

        self.stop()

        log(f"Starting chip-tool: {CHIP_TOOL} interactive start")

        try:
            # This Python bridge itself runs as root.
            # Do not invoke sudo or sudo -n inside pexpect.
            self.process = pexpect.spawn(
                CHIP_TOOL,
                ["interactive", "start"],
                encoding="utf-8",
                timeout=START_TIMEOUT_SECONDS,
            )

            result = self.process.expect(
                [PROMPT, pexpect.EOF, pexpect.TIMEOUT],
                timeout=START_TIMEOUT_SECONDS,
            )

            if result == 0:
                log("chip-tool interactive prompt detected; child is ready.")
                return

            output = clean_output(self.process.before)
            self.stop()

            if result == 1:
                raise ChipToolError(
                    "chip-tool exited before displaying its interactive prompt.",
                    output,
                )

            raise ChipToolError(
                f"Timed out waiting for chip-tool prompt {PROMPT!r}.",
                output,
            )

        except pexpect.ExceptionPexpect as error:
            output = ""

            if self.process is not None:
                output = clean_output(self.process.before)

            self.stop()

            raise ChipToolError(
                f"Could not start chip-tool: {type(error).__name__}: {error}",
                output,
            ) from error

    def stop(self):
        """Request a clean exit, then force-stop chip-tool if required."""
        if self.process is None:
            return

        log("Stopping chip-tool interactive child.")

        try:
            if self.process.isalive():
                self.process.sendline("exit")

                try:
                    self.process.expect(pexpect.EOF, timeout=3)
                except (pexpect.TIMEOUT, pexpect.EOF):
                    pass

            if self.process.isalive():
                self.process.terminate(force=True)

        except Exception as error:
            log(
                f"Warning while stopping chip-tool: "
                f"{type(error).__name__}: {error}"
            )

            try:
                self.process.terminate(force=True)
            except Exception:
                pass

        finally:
            self.process = None

    @staticmethod
    def output_indicates_failure(output):
        """Detect common Matter/chip-tool command failure output."""
        markers = (
            "Run command failure",
            "CHIP Error",
            "Command failed",
            "No operational device",
            "No route to host",
            "Connection refused",
            "Network is unreachable",
            "Interaction model status: Failure",
            "Interaction model status: Unsupported",
            "Interaction model status: InvalidCommand",
            "Interaction model status: Timeout",
            "Timed out",
            "Status=0x01",
            "Status=0x85",
        )

        output_lower = output.lower()

        return any(marker.lower() in output_lower for marker in markers)

    def _run_once(self, command):
        """
        Send one chip-tool command and wait for the real interactive prompt.

        The prompt regex is anchored to the beginning of a line, preventing
        false matches against Matter debug-log entries containing '[EM] >>>'.
        """
        if not self.is_alive():
            log("chip-tool child is not alive; starting a replacement.")
            self.start()

        try:
            log(f"REQUESTED CHIP-TOOL COMMAND: {command}")

            self.process.sendline(command)

            result = self.process.expect(
                [PROMPT, pexpect.EOF, pexpect.TIMEOUT],
                timeout=COMMAND_TIMEOUT_SECONDS,
            )

            output = clean_output(self.process.before)

            if result == 1:
                raise ChipToolError(
                    f"chip-tool exited while running '{command}'.",
                    output,
                )

            if result == 2:
                raise ChipToolError(
                    f"Timed out waiting for the real interactive prompt "
                    f"after '{command}'.",
                    output,
                )

            # Reaching the actual prompt means chip-tool completed processing
            # the command. Reject explicit failure output.
            if self.output_indicates_failure(output):
                raise ChipToolError(
                    "chip-tool reported a command failure.",
                    output,
                )

            log(
                "CHIP-TOOL RESULT: "
                f"real_prompt_seen=yes; output_chars={len(output)}"
            )

            return output

        except pexpect.ExceptionPexpect as error:
            output = ""

            if self.process is not None:
                output = clean_output(self.process.before)

            raise ChipToolError(
                f"pexpect error while running '{command}': {error}",
                output,
            ) from error

    def run(self, command):
        """
        Run a chip-tool command.

        If the first attempt fails, restart chip-tool and retry exactly once.
        """
        with self.lock:
            last_error = None
            last_output = ""

            for attempt in (1, 2):
                try:
                    output = self._run_once(command)

                    return {
                        "success": True,
                        "command": command,
                        "attempts": attempt,
                        "raw_output": output,
                    }

                except ChipToolError as error:
                    last_error = str(error)
                    last_output = error.output

                    log(f"Command attempt {attempt} failed: {last_error}")

                    self.stop()

                    if attempt == 1:
                        log("Restarting chip-tool and retrying once.")
                        time.sleep(0.5)

            return {
                "success": False,
                "command": command,
                "attempts": 2,
                "raw_output": last_output,
                "error": (
                    "Command failed after one chip-tool restart and retry. "
                    f"{last_error}"
                ),
            }

    def switch(self, action):
        """Turn the configured Matter plug on or off."""
        if action not in ("on", "off"):
            raise ValueError("action must be 'on' or 'off'")

        return self.run(
            f"onoff {action} {self.node_id} {self.endpoint_id}"
        )

    def read_onoff(self):
        """Read the configured Matter plug's OnOff attribute."""
        return self.run(
            f"onoff read on-off {self.node_id} {self.endpoint_id}"
        )


# =================================================================
# Flask HTTP API
# =================================================================

app = Flask(__name__)

chip = PersistentChipTool(
    node_id=ARGS.node_id,
    endpoint_id=ARGS.endpoint_id,
)

atexit.register(chip.stop)


def switch_response(action):
    """Return a response for POST /plug/on or POST /plug/off."""
    log(f"HTTP REQUEST: POST /plug/{action}")

    started = time.monotonic()
    result = chip.switch(action)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    # Concise successful response.
    payload = {
        "success": result["success"],
        "action": action.upper(),
        "node_id": ARGS.node_id,
        "endpoint_id": ARGS.endpoint_id,
        "attempts": result.get("attempts", 1),
        "elapsed_ms": elapsed_ms,
        "timestamp": utc_timestamp(),
    }

    if result["success"]:
        return jsonify(payload), 200

    # Include diagnostics only on an error.
    payload["error"] = result.get("error", "chip-tool command failed")

    if result.get("raw_output"):
        payload["chip_tool_output"] = result["raw_output"]

    return jsonify(payload), 502


@app.post("/plug/on")
def turn_on():
    return switch_response("on")


@app.post("/plug/off")
def turn_off():
    return switch_response("off")


@app.route("/plug/status", methods=["GET", "POST"])
def plug_status():
    """
    Read the Matter OnOff attribute.

    GET is the intended API method.
    POST is also accepted for compatibility with earlier tests.
    """
    log(f"HTTP REQUEST: {request.method} /plug/status")

    started = time.monotonic()
    result = chip.read_onoff()
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    if not result["success"]:
        return jsonify({
            "success": False,
            "node_id": ARGS.node_id,
            "endpoint_id": ARGS.endpoint_id,
            "attempts": result.get("attempts", 1),
            "elapsed_ms": elapsed_ms,
            "timestamp": utc_timestamp(),
            "error": result.get("error", "chip-tool status read failed"),
            "chip_tool_output": result.get("raw_output", ""),
        }), 502

    output = result.get("raw_output", "")

    # Supports observed output:
    # OnOff: TRUE
    # Data = true,
    #
    # Also supports common alternate chip-tool output forms.
    status_patterns = (
        r"\bOnOff\b\s*[:=]\s*(true|false|0|1)\b",
        r"\bData\b\s*[:=]\s*(true|false|0|1)\b",
        r"\bvalue\b\s*[:=]\s*(true|false|0|1)\b",
        r"\bboolean\b\s*[:=]\s*(true|false|0|1)\b",
    )

    on_value = None

    for pattern in status_patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE)

        if match:
            on_value = match.group(1).lower() in ("true", "1")
            break

    if on_value is None:
        return jsonify({
            "success": False,
            "node_id": ARGS.node_id,
            "endpoint_id": ARGS.endpoint_id,
            "attempts": result.get("attempts", 1),
            "elapsed_ms": elapsed_ms,
            "timestamp": utc_timestamp(),
            "error": (
                "chip-tool completed the status read but returned no "
                "parseable OnOff value"
            ),
            "chip_tool_output": output,
        }), 502

    # Concise successful status response.
    return jsonify({
        "success": True,
        "node_id": ARGS.node_id,
        "endpoint_id": ARGS.endpoint_id,
        "on": on_value,
        "attempts": result.get("attempts", 1),
        "elapsed_ms": elapsed_ms,
        "timestamp": utc_timestamp(),
    }), 200


# =================================================================
# Server startup
# =================================================================

if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit(
            "\nStart this service as root because chip-tool controller "
            "state is root-owned.\n\n"
            "Generic startup pattern:\n"
            "sudo /path/to/venv/bin/python /path/to/p110mcontrol.py "
            "--node-id 1 --endpoint-id 1 "
            "--listen-host 127.0.0.1 --listen-port 8080\n"
        )

    # Fail immediately if chip-tool cannot initialize.
    chip.start()

    print("==================================================")
    print(" Persistent chip-tool HTTP API running")
    print(f" Listening: http://{ARGS.listen_host}:{ARGS.listen_port}")
    print(f" Node ID: {ARGS.node_id}; Endpoint ID: {ARGS.endpoint_id}")
    print("==================================================", flush=True)

    serve(
        app,
        host=ARGS.listen_host,
        port=ARGS.listen_port,
        threads=4,
    )