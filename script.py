#!/usr/bin/env python3
"""flowTech-x/unlock-tool - diagnostic version.

Based on https://github.com/flowTech-x/unlock-tool/blob/main/script.py
Keeps the original slot selection, timing, and apply workflow, while
making the previously silent status/HTTP errors visible. The Xiaomi token
and raw API response are intentionally not printed.
"""

# =========================
# CONFIG - BEIJING TIMES ONLY
# =========================
SKIP_TIMING = False          # False = next Beijing midnight, True = manual
MANUAL_FIRE_HOUR = 19        # Beijing hour
MANUAL_FIRE_MIN = 37         # Beijing minute
MANUAL_FIRE_SEC = 0          # Beijing second
OFFSET_MS = 120              # First shot X ms BEFORE midnight
BURST_INTERVAL_MS = 50       # 50 ms between shots

# Diagnostic options
DEBUG = True                 # Show status and HTTP diagnostics
STATUS_ONLY = False          # True = check status only, no unlock requests

# =========================
# IMPORTS / DEPENDENCIES
# =========================
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])


# Install missing dependencies BEFORE importing them.
for package in ("ntplib", "pytz", "urllib3", "colorama"):
    try:
        __import__(package)
    except ImportError:
        print(f"[SETUP] Installing missing package: {package}", flush=True)
        install(package)

import ntplib
import pytz
import urllib3
from colorama import Fore, Style, init

# =========================
# SETUP
# =========================
init(autoreset=True)
G, Y, R, B = Fore.GREEN, Fore.YELLOW, Fore.RED, Fore.BLUE
GB = Style.BRIGHT + Fore.GREEN

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = SCRIPT_DIR / "token.txt"


def debug(message):
    if DEBUG:
        print(f"[DEBUG] {message}", flush=True)


def clear():
    # Do not clear the screen while debugging, as that hides useful output.
    if not DEBUG:
        os.system("cls" if os.name == "nt" else "clear")


def signal_handler(sig, frame):
    print(f"\n{R}[!] Interrupted - Clean exit", flush=True)
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)

# =========================
# BEIJING TIME
# =========================
def show_beijing_time():
    beijing = datetime.now(timezone.utc).astimezone(pytz.timezone("Asia/Shanghai"))
    print(f'{B}[BEIJING] {beijing.strftime("%H:%M:%S.%f")[:-3]}{Fore.RESET}', flush=True)


# =========================
# CONSTANTS
# =========================
URL_STATUS = "https://sgp-api.buy.mi.com/bbs/api/global/user/bl-switch/state"
URL_APPLY = "https://sgp-api.buy.mi.com/bbs/api/global/apply/bl-auth"
UA = "okhttp/4.12.0"
NTP_SERVERS = [
    "ntp0.ntp-servers.net",
    "ntp1.ntp-servers.net",
    "ntp2.ntp-servers.net",
]

# =========================
# HELPERS
# =========================
def gen_device_id():
    return hashlib.sha1(f"{random.random()}-{time.time()}".encode()).hexdigest().upper()


def get_ntp_beijing():
    tz = pytz.timezone("Asia/Shanghai")
    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            result = client.request(server, version=3, timeout=3)
            bt = datetime.fromtimestamp(result.tx_time, timezone.utc).astimezone(tz)
            print(f'{G}[NTP] {bt.strftime("%H:%M:%S.%f")[:-3]}{Fore.RESET}')
            return bt
        except Exception as exc:
            debug(f"NTP {server} failed: {type(exc).__name__}: {exc}")
    bt = datetime.now(timezone.utc).astimezone(tz)
    print(f'{Y}[SYS] {bt.strftime("%H:%M:%S.%f")[:-3]}{Fore.RESET}')
    return bt


def synced_time(start_bt, start_ts):
    return start_bt + timedelta(seconds=(time.time() - start_ts))


# =========================
# PERFECT TIMING (ORIGINAL WORKFLOW)
# =========================
def get_target(start_bt):
    if SKIP_TIMING:
        target = start_bt.replace(
            hour=MANUAL_FIRE_HOUR,
            minute=MANUAL_FIRE_MIN,
            second=MANUAL_FIRE_SEC,
            microsecond=0,
        )
        print(f'{Y}[MANUAL] {target.strftime("%H:%M:%S Beijing")}')
        return target

    target = (start_bt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    print(f'{Y}[AUTO] Midnight target: {target.strftime("%H:%M:%S Beijing")}')
    return target


def wait_target(start_bt, start_ts, target):
    print(f'{Y}Waiting for exact target...', flush=True)
    last_show = 0
    while True:
        now = synced_time(start_bt, start_ts)
        diff = (target - now).total_seconds()
        if time.time() - last_show > 1:
            show_beijing_time()
            last_show = time.time()
        if diff > 0.01:
            time.sleep(min(diff - 0.005, 0.1))
        elif diff > 0:
            time.sleep(0.0001)
        else:
            break


# =========================
# HTTP SESSION
# =========================
class Session:
    def __init__(self):
        self.http = urllib3.PoolManager(
            maxsize=10,
            retries=urllib3.Retry(total=1, backoff_factor=0),
            timeout=urllib3.Timeout(connect=2.0, read=5.0),
        )

    def request(self, method, url, headers=None, body=None):
        try:
            h = dict(headers or {})
            if method == "POST":
                body = body or b'{"is_retry":true}'
                h.update({
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "User-Agent": UA,
                    "Connection": "keep-alive",
                    "Accept-Encoding": "gzip",
                })
            debug(f"Sending {method} request to {url} (no credentials logged)")
            # Same HTTP transport settings as the upstream script.
            response = self.http.request(
                method, url, headers=h, body=body, preload_content=False
            )
            debug(f"HTTP response status: {response.status}")
            return response
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f'{R}[HTTP ERR] {type(exc).__name__}: {exc}', flush=True)
            return None


# =========================
# STATUS & FIRE
# =========================
def cookie_headers(token, device_id):
    return {
        "Cookie": (
            f"new_bbs_serviceToken={token};versionCode=500411;"
            f"versionName=5.4.11;deviceId={device_id};"
        )
    }


def parse_response(response):
    """Read JSON and release the connection in all cases."""
    try:
        raw = response.data
        return json.loads(raw.decode("utf-8"))
    finally:
        response.release_conn()


def check_status(session, token, device_id):
    debug("Starting status check (check_status is running)")
    response = session.request("GET", URL_STATUS, headers=cookie_headers(token, device_id))
    if response is None:
        print(f"{R}[Status]: Could not reach Xiaomi status API", flush=True)
        return False

    try:
        data = parse_response(response)
    except (ValueError, UnicodeError) as exc:
        print(f"{R}[Status]: Invalid JSON response: {type(exc).__name__}: {exc}", flush=True)
        return False
    except Exception as exc:
        print(f"{R}[Status]: Failed to read response: {type(exc).__name__}: {exc}", flush=True)
        return False

    if not isinstance(data, dict):
        print(f"{R}[Status]: Unexpected API response type: {type(data).__name__}", flush=True)
        return False

    info = data.get("data")
    if not isinstance(info, dict):
        debug(f"API 'data' is {type(info).__name__}, not a dictionary")
        info = {}

    code = data.get("code")
    is_pass = info.get("is_pass")
    button = info.get("button_state")

    # Do NOT print the token, request cookie, or raw response.
    debug(f"API code: {code!r}")
    debug(f"is_pass: {is_pass!r}")
    debug(f"button_state: {button!r}")
    debug(f"Available top-level JSON fields: {list(data.keys())}")
    debug(f"Available 'data' fields: {list(info.keys())}")

    if response.status != 200:
        print(f"{R}[Status]: HTTP {response.status} (unexpected response)", flush=True)
        return False
    if code == 100004:
        print(f"{R}[Status]: Token expired!", flush=True)
        return False
    if is_pass == 4 and button == 1:
        print(f"{GB}[Status]: READY!", flush=True)
        return True
    if is_pass == 1:
        print(f"{GB}[Status]: Already approved!", flush=True)
        return False

    print(f"{R}[Status]: Not ready", flush=True)
    print(f"{Y}[HINT] Xiaomi did not return the expected is_pass=4 and button_state=1.", flush=True)
    print(f"{Y}[HINT] Share only the DEBUG values (never token.txt or cookies).", flush=True)
    return False


def fire(session, token, device_id):
    response = session.request("POST", URL_APPLY, headers=cookie_headers(token, device_id))
    if response is None:
        return None
    try:
        return parse_response(response)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"{R}[APPLY ERR] {type(exc).__name__}: {exc}", flush=True)
        return None


def handle_resp(resp):
    if not isinstance(resp, dict):
        print(f"{R}[APPLY ERR] Unexpected response type", flush=True)
        return False
    if resp.get("code") != 0:
        print(f'{R}API ERR (code={resp.get("code")!r})', flush=True)
        return False

    info = resp.get("data") or {}
    if not isinstance(info, dict):
        print(f"{R}[APPLY ERR] Invalid 'data' field", flush=True)
        return False
    result = info.get("apply_result")
    deadline = info.get("deadline_format", "")
    if result == 1:
        print(f'{GB}APPROVED!', flush=True)
        return True
    elif result == 3:
        print(f'{Y}QUOTA: {deadline}', flush=True)
        return False
    elif result == 4:
        print(f'{R}BLOCKED: {deadline}', flush=True)
        return False
    else:
        print(f'{R}Unexpected apply_result={result!r} (code={resp.get("code")!r})', flush=True)
        return False


# =========================
# MAIN - ORIGINAL STAGGERED BURST
# =========================
def main():
    clear()
    print(f"{G}Unlock Tool - Debug version", flush=True)
    debug(f"Running file: {Path(__file__).resolve()}")
    debug(f"Token file expected at: {TOKEN_FILE}")
    show_beijing_time()

    try:
        slot = int(input(f'{G}[Slot 1-4]: '))
    except (ValueError, EOFError):
        print(f'{R}Invalid slot input', flush=True)
        return

    token_num = 1 if slot in (1, 3) else 2 if slot in (2, 4) else None
    if token_num is None:
        print(f'{R}Invalid slot', flush=True)
        return

    clear()
    show_beijing_time()
    print(f'{GB}Token #{token_num}', flush=True)

    if not TOKEN_FILE.is_file():
        print(f'{R}Missing token.txt at: {TOKEN_FILE}', flush=True)
        return

    try:
        token_lines = TOKEN_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        print(f'{R}Cannot read token.txt: {type(exc).__name__}: {exc}', flush=True)
        return

    token = token_lines[token_num - 1].strip() if len(token_lines) >= token_num else ""
    if not token:
        print(f'{R}No token in line {token_num} of token.txt', flush=True)
        return

    debug("Token loaded successfully (value hidden)")
    device_id = gen_device_id()
    session = Session()
    print(f'{Y}Checking Xiaomi unlock status...', flush=True)

    if not check_status(session, token, device_id):
        return
    if STATUS_ONLY:
        print(f'{G}[STATUS_ONLY] Status check complete; no unlock request sent.', flush=True)
        return

    start_bt = get_ntp_beijing()
    start_ts = time.time()
    target = get_target(start_bt)
    wait_target(start_bt, start_ts, target - timedelta(milliseconds=OFFSET_MS))

    clear()
    show_beijing_time()
    print(f'{GB}PRECISE 10x BURST STARTS NOW!{Fore.RESET}\n', flush=True)

    success = False
    try:
        for i in range(10):
            shot_target = target + timedelta(milliseconds=i * BURST_INTERVAL_MS)
            while True:
                shot_time = synced_time(start_bt, start_ts)
                diff = (shot_target - shot_time).total_seconds()
                if diff <= 0:
                    break
                time.sleep(diff * 0.8)

            now_beijing = datetime.now(timezone.utc).astimezone(pytz.timezone("Asia/Shanghai"))
            print(f'[{i + 1}/10] ', end='', flush=True)
            resp = fire(session, token, device_id)
            if resp:
                print(f'{B}@{now_beijing.strftime("%H:%M:%S.%f")}{Fore.RESET} ', end='', flush=True)
                if handle_resp(resp):
                    success = True
                    print(f'\n{GB}SUCCESS on shot {i + 1}!', flush=True)
                    break
    except KeyboardInterrupt:
        print(f'\n{Y}[!] Burst interrupted', flush=True)

    if not success:
        print(f'{R}\nBurst complete - No approval', flush=True)
        print(f'{Y}[*] Next quota: 00:00 Beijing tomorrow', flush=True)


if __name__ == "__main__":
    main()
