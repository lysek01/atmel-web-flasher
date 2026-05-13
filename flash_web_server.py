from __future__ import annotations

import json
import platform
import shlex
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from flask import Flask, jsonify, render_template_string, request

# ============================================================
# ATMEL / MICROCHIP WEB FLASHER
# ============================================================
# Install:
#   pip install flask
#
# Run:
#   python flash_web_server.py
#
# On a phone/tablet open:
#   http://PC_IP_ADDRESS:5000
#
# Features:
#   - big web FLASH button
#   - automatic list of available programmers via `atprogram list`
#   - pick tool, serial, interface, MCU, path to the HEX file
#   - live preview of the full command line
#   - settings persisted to config.json on the PC
#   - logs written to the flash_logs folder
# ============================================================

PORT = 5000
CONFIG_FILE = Path("config.json")
LOG_DIR = Path("flash_logs")

# Optional simple password.
# Leave empty "" to disable.
WEB_PASSWORD = ""

# When True, FLASH only sleeps for 3 seconds and programs nothing.
# When False, the real atprogram command is executed.
DEFAULT_TEST_MODE = True

# Hard limit for the real flashing subprocess so the UI cannot hang forever
# if the programmer or target stops responding.
FLASH_TIMEOUT_SEC = 300

DEFAULT_CONFIG = {
    "test_mode": DEFAULT_TEST_MODE,
    "atprogram": "atprogram",
    "tool": "atmelice",
    "serial": "J42700049573",
    "interface": "updi",
    "device": "ATtiny3227",
    "hex_file": r"C:\Users\marti\Documents\GitHub\zoneiot_light\software\zoneiot_light\Debug\zoneiot_light.hex",
    "verify": True,
    "extra_args": "",
}

COMMON_INTERFACES = ["updi", "isp", "jtag", "swd", "debugwire", "pdi", "tpi"]
COMMON_DEVICES = [
    "ATtiny3227",
    "ATtiny3217",
    "ATtiny1616",
    "ATtiny1606",
    "ATtiny1626",
    "ATtiny1627",
    "ATmega4809",
    "ATmega3208",
    "ATmega328P",
    "AVR64DA32",
    "AVR64DB32",
    "AVR128DA32",
    "AVR128DB32",
]

StatusState = Literal["READY", "FLASHING", "ERROR"]

app = Flask(__name__)
lock = threading.Lock()

# Sentinel used by set_status() so callers can distinguish "do not touch"
# from "set this field to None".
_UNSET = object()

status = {
    "state": "READY",
    "log": "",
    "last_exit_code": None,
    "last_started": None,
    "last_finished": None,
    "last_duration_sec": None,
    "last_log_file": None,
}

config: dict = {}

HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <title>Atmel Web Flasher</title>
    <style>
        :root {
            --bg: #0f1115;
            --panel: #171a21;
            --panel2: #20242e;
            --text: #f2f4f8;
            --muted: #9aa4b2;
            --ready: #00d26a;
            --busy: #ffb020;
            --error: #ff4d4d;
            --button: #2d7dff;
            --button-active: #1b5fd0;
            --disabled: #4a4f5a;
            --border: rgba(255,255,255,0.10);
        }

        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }

        body {
            margin: 0;
            min-height: 100vh;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #1d2330 0, var(--bg) 45%, #08090c 100%);
            color: var(--text);
        }

        .wrap {
            width: 100%;
            max-width: 900px;
            margin: 0 auto;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .card {
            background: rgba(23, 26, 33, 0.94);
            border: 1px solid var(--border);
            border-radius: 26px;
            padding: 18px;
            box-shadow: 0 18px 50px rgba(0,0,0,0.35);
        }

        h1 { margin: 0 0 4px 0; font-size: 28px; }
        h2 { margin: 0 0 14px 0; font-size: 20px; }
        .sub { color: var(--muted); font-size: 14px; }

        #status {
            margin: 16px 0 6px 0;
            font-size: clamp(50px, 15vw, 92px);
            line-height: 1;
            font-weight: 900;
            text-align: center;
            letter-spacing: 2px;
        }

        #detail { text-align: center; color: var(--muted); min-height: 24px; font-size: 16px; }
        .ready { color: var(--ready); }
        .flashing { color: var(--busy); }
        .error { color: var(--error); }

        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }

        label {
            display: block;
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 6px;
        }

        input, select, textarea {
            width: 100%;
            border-radius: 15px;
            border: 1px solid var(--border);
            background: #0a0c10;
            color: white;
            padding: 12px;
            font-size: 16px;
            outline: none;
        }

        textarea {
            min-height: 78px;
            resize: vertical;
            font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
            font-size: 13px;
        }

        .check-row {
            display: flex;
            align-items: center;
            gap: 10px;
            min-height: 46px;
        }

        .check-row input { width: 22px; height: 22px; }
        .check-row label { margin: 0; font-size: 16px; color: var(--text); }

        .buttons {
            display: grid;
            grid-template-columns: 1fr;
            gap: 12px;
        }

        .row-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 10px;
        }

        @media (max-width: 620px) { .row-buttons { grid-template-columns: 1fr; } }

        button {
            width: 100%;
            border: 0;
            border-radius: 22px;
            padding: 18px 16px;
            font-size: 22px;
            font-weight: 800;
            color: white;
            background: linear-gradient(180deg, #3b8cff, var(--button));
            box-shadow: 0 12px 30px rgba(45,125,255,0.26);
            cursor: pointer;
            touch-action: manipulation;
        }

        #flashBtn {
            padding: 28px 22px;
            font-size: 42px;
            border-radius: 28px;
        }

        button:active { transform: translateY(1px); background: var(--button-active); }
        button:disabled { background: var(--disabled); box-shadow: none; color: #c7ccd6; cursor: not-allowed; }
        .secondary { background: var(--panel2); box-shadow: none; font-size: 18px; }
        .danger { background: #743030; box-shadow: none; }

        pre {
            margin: 0;
            width: 100%;
            min-height: 150px;
            max-height: 42vh;
            overflow: auto;
            background: #0a0c10;
            border-radius: 18px;
            padding: 14px;
            color: #dbe3ee;
            font-size: 13px;
            line-height: 1.45;
            white-space: pre-wrap;
            word-break: break-word;
        }

        .command {
            min-height: 76px;
            color: #bde0ff;
        }

        .password { display: none; margin-top: 12px; }

        .toast {
            position: fixed;
            left: 50%;
            bottom: 22px;
            transform: translateX(-50%);
            background: #222837;
            color: white;
            padding: 12px 16px;
            border-radius: 999px;
            font-size: 15px;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease;
            max-width: calc(100vw - 28px);
            text-align: center;
            z-index: 10;
        }

        .toast.show { opacity: 1; }
        .hint { color: var(--muted); font-size: 13px; margin-top: 8px; line-height: 1.35; }
        .inline { display: flex; gap: 8px; align-items: end; }
        .inline > div { flex: 1; }
        .inline > button { width: auto; min-width: 120px; padding: 12px 14px; font-size: 16px; border-radius: 15px; }
    </style>
</head>
<body>
    <div class="wrap">
        <section class="card">
            <h1>Atmel Web Flasher</h1>
            <div class="sub" id="serverInfo">Loading...</div>
            <div id="status" class="ready">READY</div>
            <div id="detail">Ready</div>
            <div class="password" id="passwordBox">
                <label>Password</label>
                <input id="password" type="password" placeholder="Password">
            </div>
        </section>

        <section class="buttons">
            <button id="flashBtn" onclick="flashNow()">FLASH</button>
            <div class="row-buttons">
                <button class="secondary" onclick="saveConfig()">SAVE</button>
                <button class="secondary" onclick="refreshTools()">DETECT TOOLS</button>
                <button class="secondary" onclick="copyCommand()">COPY CMD</button>
            </div>
        </section>

        <section class="card">
            <h2>Command settings</h2>
            <div class="grid">
                <div>
                    <label>atprogram</label>
                    <input id="atprogram" value="atprogram" oninput="onConfigChanged()">
                </div>
                <div>
                    <label>Detected programmer</label>
                    <select id="toolSelect" onchange="selectToolFromList()">
                        <option value="">Use &bdquo;DETECT TOOLS&ldquo;</option>
                    </select>
                </div>
                <div>
                    <label>Tool</label>
                    <input id="tool" list="toolList" oninput="onConfigChanged()" placeholder="e.g. atmelice">
                    <datalist id="toolList">
                        <option value="atmelice"></option>
                        <option value="snap"></option>
                        <option value="pickit4"></option>
                        <option value="edbg"></option>
                        <option value="medbg"></option>
                        <option value="simulator"></option>
                    </datalist>
                </div>
                <div>
                    <label>Serial</label>
                    <input id="serial" oninput="onConfigChanged()" placeholder="e.g. J42700049573">
                </div>
                <div>
                    <label>Interface</label>
                    <input id="interface" list="interfaceList" oninput="onConfigChanged()" placeholder="e.g. updi">
                    <datalist id="interfaceList"></datalist>
                </div>
                <div>
                    <label>MCU / Device</label>
                    <input id="device" list="deviceList" oninput="onConfigChanged()" placeholder="e.g. ATtiny3227">
                    <datalist id="deviceList"></datalist>
                </div>
            </div>

            <div style="margin-top: 12px;">
                <label>Path to the HEX file on the PC</label>
                <input id="hex_file" oninput="onConfigChanged()" placeholder="C:\\...\\firmware.hex">
                <div class="hint">Note: a phone cannot browse the PC's filesystem, so the path is entered as text and stored in config.json on the PC.</div>
            </div>

            <div class="grid" style="margin-top: 12px;">
                <div class="check-row">
                    <input id="verify" type="checkbox" onchange="onConfigChanged()">
                    <label for="verify">--verify</label>
                </div>
                <div class="check-row">
                    <input id="test_mode" type="checkbox" onchange="onConfigChanged()">
                    <label for="test_mode">TEST MODE</label>
                </div>
            </div>

            <div style="margin-top: 12px;">
                <label>Extra arguments</label>
                <input id="extra_args" oninput="onConfigChanged()" placeholder="optional, e.g. --clock 500khz">
            </div>
        </section>

        <section class="card">
            <h2>Command preview</h2>
            <pre id="commandPreview" class="command"></pre>
            <div class="hint" id="configSavedInfo"></div>
        </section>

        <section class="card">
            <h2>Log</h2>
            <div class="sub" id="exitInfo" style="margin-bottom: 10px;"></div>
            <pre id="log"></pre>
        </section>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let currentConfig = null;
        let passwordRequired = false;
        let saveTimer = null;

        function getPassword() {
            const el = document.getElementById("password");
            return el ? el.value : "";
        }

        function showToast(text) {
            const toast = document.getElementById("toast");
            toast.textContent = text;
            toast.classList.add("show");
            setTimeout(() => toast.classList.remove("show"), 1900);
        }

        function readFormConfig() {
            return {
                atprogram: document.getElementById("atprogram").value.trim() || "atprogram",
                tool: document.getElementById("tool").value.trim(),
                serial: document.getElementById("serial").value.trim(),
                interface: document.getElementById("interface").value.trim(),
                device: document.getElementById("device").value.trim(),
                hex_file: document.getElementById("hex_file").value.trim(),
                verify: document.getElementById("verify").checked,
                test_mode: document.getElementById("test_mode").checked,
                extra_args: document.getElementById("extra_args").value.trim(),
            };
        }

        function applyConfig(cfg) {
            currentConfig = cfg;
            document.getElementById("atprogram").value = cfg.atprogram || "atprogram";
            document.getElementById("tool").value = cfg.tool || "";
            document.getElementById("serial").value = cfg.serial || "";
            document.getElementById("interface").value = cfg.interface || "";
            document.getElementById("device").value = cfg.device || "";
            document.getElementById("hex_file").value = cfg.hex_file || "";
            document.getElementById("verify").checked = !!cfg.verify;
            document.getElementById("test_mode").checked = !!cfg.test_mode;
            document.getElementById("extra_args").value = cfg.extra_args || "";
            updateCommandPreview();
        }

        function shellQuoteWin(s) {
            if (!s) return "";
            if (/^[A-Za-z0-9_:./-]+$/.test(s)) return s;
            return '"' + s.replace(/"/g, '\\"') + '"';
        }

        function buildCommandClient(cfg) {
            const parts = [];
            parts.push(shellQuoteWin(cfg.atprogram || "atprogram"));
            if (cfg.tool) parts.push("-t", cfg.tool);
            if (cfg.serial) parts.push("-s", cfg.serial);
            if (cfg.interface) parts.push("-i", cfg.interface);
            if (cfg.device) parts.push("-d", cfg.device);
            parts.push("program");
            if (cfg.hex_file) parts.push("-f", shellQuoteWin(cfg.hex_file));
            if (cfg.verify) parts.push("--verify");
            if (cfg.extra_args) parts.push(cfg.extra_args);
            return parts.join(" ");
        }

        function updateCommandPreview() {
            const cfg = readFormConfig();
            const preview = cfg.test_mode
                ? "TEST MODE: real flashing is disabled.\\n\\n" + buildCommandClient(cfg)
                : buildCommandClient(cfg);
            document.getElementById("commandPreview").textContent = preview;
        }

        function onConfigChanged() {
            updateCommandPreview();
            document.getElementById("configSavedInfo").textContent = "Unsaved changes.";
            clearTimeout(saveTimer);
            saveTimer = setTimeout(() => saveConfig(false), 900);
        }

        async function loadConfig() {
            try {
                const res = await fetch("/config");
                const data = await res.json();
                applyConfig(data.config);
                fillLists(data.common_interfaces, data.common_devices);
                document.getElementById("configSavedInfo").textContent = data.config_file ? `Config file: ${data.config_file}` : "";
            } catch (e) {
                showToast("Could not load config");
            }
        }

        function fillLists(interfaces, devices) {
            const il = document.getElementById("interfaceList");
            il.innerHTML = "";
            interfaces.forEach(x => {
                const opt = document.createElement("option");
                opt.value = x;
                il.appendChild(opt);
            });

            const dl = document.getElementById("deviceList");
            dl.innerHTML = "";
            devices.forEach(x => {
                const opt = document.createElement("option");
                opt.value = x;
                dl.appendChild(opt);
            });
        }

        async function saveConfig(show = true) {
            const cfg = readFormConfig();
            try {
                const res = await fetch("/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password: getPassword(), config: cfg })
                });
                const data = await res.json();
                if (!data.ok) {
                    showToast(data.message || "Could not save config");
                    return;
                }
                currentConfig = data.config;
                document.getElementById("configSavedInfo").textContent = `Saved: ${data.config_file}`;
                if (show) showToast("Settings saved");
            } catch (e) {
                showToast("Could not save config");
            }
        }

        async function refreshTools() {
            const cfg = readFormConfig();
            try {
                const res = await fetch("/tools", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password: getPassword(), atprogram: cfg.atprogram })
                });
                const data = await res.json();
                const sel = document.getElementById("toolSelect");
                sel.innerHTML = "";

                if (!data.ok || !data.tools || !data.tools.length) {
                    const opt = document.createElement("option");
                    opt.value = "";
                    opt.textContent = "No tools found";
                    sel.appendChild(opt);
                    showToast(data.message || "No tools found");
                    return;
                }

                data.tools.forEach(t => {
                    const opt = document.createElement("option");
                    opt.value = JSON.stringify(t);
                    opt.textContent = t.serial ? `${t.tool} / ${t.serial}` : `${t.tool}`;
                    sel.appendChild(opt);
                });

                showToast("Tools detected");
            } catch (e) {
                showToast("Could not detect tools");
            }
        }

        function selectToolFromList() {
            const val = document.getElementById("toolSelect").value;
            if (!val) return;
            try {
                const t = JSON.parse(val);
                document.getElementById("tool").value = t.tool || "";
                document.getElementById("serial").value = t.serial || "";
                onConfigChanged();
            } catch (e) {}
        }

        async function flashNow() {
            const btn = document.getElementById("flashBtn");
            btn.disabled = true;
            await saveConfig(false);

            try {
                const res = await fetch("/flash", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password: getPassword() })
                });

                const data = await res.json();
                if (!data.ok) showToast(data.message || "Could not start flashing");
            } catch (e) {
                showToast("Cannot reach the PC server");
            }

            await updateStatus(true);
        }

        async function updateStatus(forceToast = false) {
            try {
                const res = await fetch("/status");
                const data = await res.json();

                passwordRequired = data.password_required;
                document.getElementById("passwordBox").style.display = passwordRequired ? "block" : "none";

                const status = document.getElementById("status");
                const btn = document.getElementById("flashBtn");
                const detail = document.getElementById("detail");
                const log = document.getElementById("log");
                const exitInfo = document.getElementById("exitInfo");
                const serverInfo = document.getElementById("serverInfo");

                status.textContent = data.state;
                status.className = "";

                if (data.state === "READY") {
                    status.classList.add("ready");
                    btn.disabled = false;
                    detail.textContent = data.last_duration_sec !== null ? `Done in ${data.last_duration_sec.toFixed(2)} s` : "Ready";
                } else if (data.state === "FLASHING") {
                    status.classList.add("flashing");
                    btn.disabled = true;
                    detail.textContent = "Flashing in progress...";
                } else {
                    status.classList.add("error");
                    btn.disabled = false;
                    detail.textContent = "An error occurred";
                }

                log.textContent = data.log || "";
                exitInfo.textContent = data.last_exit_code === null ? "" : `Exit code: ${data.last_exit_code}`;
                serverInfo.textContent = data.test_mode ? "TEST MODE - real flashing is disabled" : "LIVE MODE - the real command will be executed";
            } catch (e) {
                const status = document.getElementById("status");
                status.textContent = "OFFLINE";
                status.className = "error";
                document.getElementById("flashBtn").disabled = true;
                document.getElementById("detail").textContent = "Cannot reach the PC server";
                if (forceToast) showToast("PC server is not responding");
            }
        }

        async function copyCommand() {
            const text = document.getElementById("commandPreview").textContent;
            try {
                await navigator.clipboard.writeText(text);
                showToast("Command copied");
            } catch (e) {
                showToast("Clipboard is not available");
            }
        }

        loadConfig().then(refreshTools);
        setInterval(updateStatus, 500);
        updateStatus();
    </script>
</body>
</html>
"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_CONFIG)
        merged.update(loaded)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> dict:
    clean = dict(DEFAULT_CONFIG)
    clean.update({k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()})

    clean["verify"] = bool(clean.get("verify"))
    clean["test_mode"] = bool(clean.get("test_mode"))

    CONFIG_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def set_status(
    state: StatusState | None = None,
    log=_UNSET,
    last_exit_code=_UNSET,
    last_started=_UNSET,
    last_finished=_UNSET,
    last_duration_sec=_UNSET,
    last_log_file=_UNSET,
):
    with lock:
        if state is not None:
            status["state"] = state
        if log is not _UNSET:
            status["log"] = log
        if last_exit_code is not _UNSET:
            status["last_exit_code"] = last_exit_code
        if last_started is not _UNSET:
            status["last_started"] = last_started
        if last_finished is not _UNSET:
            status["last_finished"] = last_finished
        if last_duration_sec is not _UNSET:
            status["last_duration_sec"] = last_duration_sec
        if last_log_file is not _UNSET:
            status["last_log_file"] = last_log_file


def append_log(text: str):
    with lock:
        status["log"] += text


def get_status_copy() -> dict:
    with lock:
        data = dict(status)
    data["test_mode"] = bool(config.get("test_mode", True))
    data["password_required"] = bool(WEB_PASSWORD)
    return data


def check_password(req) -> bool:
    if not WEB_PASSWORD:
        return True
    data = req.get_json(silent=True) or {}
    return data.get("password") == WEB_PASSWORD


def save_log_to_file(log_text: str) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("flash_%Y-%m-%d_%H-%M-%S.log")
    path = LOG_DIR / filename
    path.write_text(log_text, encoding="utf-8", errors="replace")
    return str(path)


def windows_quote(arg: str) -> str:
    if arg == "":
        return '""'
    if any(ch.isspace() for ch in arg) or any(ch in arg for ch in ['"', '&', '(', ')']):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def split_extra_args(extra: str) -> list[str]:
    if not extra.strip():
        return []
    # posix=False matches Windows-style quoting better.
    return shlex.split(extra, posix=False)


def build_command_args(cfg: dict) -> list[str]:
    args = [cfg.get("atprogram") or "atprogram"]

    if cfg.get("tool"):
        args += ["-t", str(cfg["tool"])]
    if cfg.get("serial"):
        args += ["-s", str(cfg["serial"])]
    if cfg.get("interface"):
        args += ["-i", str(cfg["interface"])]
    if cfg.get("device"):
        args += ["-d", str(cfg["device"])]

    args.append("program")

    if cfg.get("hex_file"):
        args += ["-f", str(cfg["hex_file"])]

    if cfg.get("verify"):
        args.append("--verify")

    args += split_extra_args(str(cfg.get("extra_args") or ""))
    return args


def build_command_preview(cfg: dict) -> str:
    return " ".join(windows_quote(str(x)) for x in build_command_args(cfg))


def parse_atprogram_list(output: str) -> list[dict]:
    tools = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        tool = parts[0]
        serial = ""
        if len(parts) >= 2:
            rest = " ".join(parts[1:])
            if "No serialnumber" not in rest and "No serial" not in rest:
                serial = parts[1]
        tools.append({"tool": tool, "serial": serial, "raw": line})
    return tools


def run_tool_list(atprogram: str) -> tuple[bool, list[dict], str]:
    try:
        result = subprocess.run(
            [atprogram or "atprogram", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        output = (result.stdout or "") + (result.stderr or "")
        tools = parse_atprogram_list(output)
        return result.returncode == 0, tools, output.strip()
    except Exception as e:
        return False, [], str(e)


def run_flash_worker():
    with lock:
        cfg = dict(config)

    started_monotonic = time.monotonic()
    started_text = now_text()

    set_status(
        state="FLASHING",
        log=f"[{started_text}] FLASH START\n",
        last_started=started_text,
        last_finished=None,
        last_duration_sec=None,
        last_log_file=None,
        last_exit_code=None,
    )

    append_log(f"Config file: {CONFIG_FILE.resolve()}\n")
    append_log(f"Command preview: {build_command_preview(cfg)}\n\n")

    if cfg.get("test_mode", True):
        append_log("TEST_MODE = True\n")
        append_log("Simulating flashing for 3 seconds...\n")
        time.sleep(3)
        exit_code = 0
        append_log("Test finished.\n")
    else:
        args = build_command_args(cfg)
        hex_file = str(cfg.get("hex_file") or "")

        if not hex_file:
            exit_code = -2
            append_log("ERROR: HEX file path is empty.\n")
        elif not Path(hex_file).exists():
            exit_code = -3
            append_log(f"ERROR: HEX file does not exist: {hex_file}\n")
        else:
            try:
                result = subprocess.run(
                    args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=FLASH_TIMEOUT_SEC,
                )
                exit_code = result.returncode

                if result.stdout:
                    append_log("--- STDOUT ---\n")
                    append_log(result.stdout)
                    if not result.stdout.endswith("\n"):
                        append_log("\n")

                if result.stderr:
                    append_log("--- STDERR ---\n")
                    append_log(result.stderr)
                    if not result.stderr.endswith("\n"):
                        append_log("\n")

            except subprocess.TimeoutExpired:
                exit_code = -4
                append_log(f"--- TIMEOUT ---\nFlashing exceeded {FLASH_TIMEOUT_SEC} s and was aborted.\n")
            except FileNotFoundError as e:
                exit_code = -5
                append_log("--- EXCEPTION ---\n")
                append_log(f"atprogram executable not found: {e}\n")
            except Exception as e:
                exit_code = -1
                append_log("--- EXCEPTION ---\n")
                append_log(str(e) + "\n")

    finished_text = now_text()
    duration = time.monotonic() - started_monotonic
    final_state: StatusState = "READY" if exit_code == 0 else "ERROR"

    append_log(f"\n[{finished_text}] FLASH FINISH\n")
    append_log(f"Exit code: {exit_code}\n")
    append_log(f"Duration: {duration:.2f} s\n")
    append_log(f"State: {final_state}\n")

    data = get_status_copy()
    log_file = save_log_to_file(data["log"])

    set_status(
        state=final_state,
        last_exit_code=exit_code,
        last_finished=finished_text,
        last_duration_sec=duration,
        last_log_file=log_file,
    )


@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML)


@app.route("/status", methods=["GET"])
def status_route():
    return jsonify(get_status_copy())


@app.route("/config", methods=["GET", "POST"])
def config_route():
    global config

    if request.method == "GET":
        return jsonify({
            "ok": True,
            "config": config,
            "config_file": str(CONFIG_FILE.resolve()),
            "common_interfaces": COMMON_INTERFACES,
            "common_devices": COMMON_DEVICES,
            "command_preview": build_command_preview(config),
        })

    if not check_password(request):
        return jsonify({"ok": False, "message": "Wrong password"}), 401

    data = request.get_json(silent=True) or {}
    new_cfg = data.get("config") or {}
    with lock:
        config = save_config(new_cfg)
        cfg_snapshot = dict(config)

    return jsonify({
        "ok": True,
        "config": cfg_snapshot,
        "config_file": str(CONFIG_FILE.resolve()),
        "command_preview": build_command_preview(cfg_snapshot),
    })


@app.route("/tools", methods=["POST"])
def tools_route():
    if not check_password(request):
        return jsonify({"ok": False, "message": "Wrong password"}), 401

    data = request.get_json(silent=True) or {}
    atprogram = data.get("atprogram") or config.get("atprogram") or "atprogram"
    ok, tools, message = run_tool_list(atprogram)
    return jsonify({"ok": ok, "tools": tools, "message": message})


@app.route("/flash", methods=["POST"])
def flash_route():
    if not check_password(request):
        return jsonify({"ok": False, "message": "Wrong password"}), 401

    with lock:
        if status["state"] == "FLASHING":
            return jsonify({"ok": False, "message": "Flashing is already in progress"}), 409

    thread = threading.Thread(target=run_flash_worker, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Flash started"})


@app.route("/health", methods=["GET"])
def health_route():
    return jsonify({"ok": True, "state": get_status_copy()["state"]})


if __name__ == "__main__":
    config = load_config()
    local_ip = get_local_ip()

    print("============================================================")
    print("ATMEL / MICROCHIP WEB FLASHER")
    print("============================================================")
    print(f"OS:          {platform.system()} {platform.release()}")
    print(f"PORT:        {PORT}")
    print(f"Config:      {CONFIG_FILE.resolve()}")
    print(f"TEST_MODE:   {config.get('test_mode')}")
    print(f"Local URL:   http://127.0.0.1:{PORT}")
    print(f"Network URL: http://{local_ip}:{PORT}")
    print("Command:")
    print(build_command_preview(config))
    print("============================================================")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
