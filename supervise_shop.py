"""Start and watch the local shop bot and its ngrok tunnel on Windows."""

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from admin_alerts import send_admin_message
from config import load_config
from service_status import CustomerStatusNotifier
from store import Store


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TUNNEL_URL = "https://mud-sinless-net.ngrok-free.dev"
LOCAL_PORT = 8080
CHECK_SECONDS = 5
STARTUP_SECONDS = 45
MAX_RETRY_SECONDS = 60
OUTAGE_SECONDS = 60
ALERT_REPEAT_SECONDS = 3600
ALERT_RETRY_SECONDS = 300
PUBLIC_CHECK_SECONDS = 30


def bot_probe() -> tuple[bool, bool]:
    try:
        with urlopen(f"http://127.0.0.1:{LOCAL_PORT}/healthz", timeout=2) as response:
            return response.status == 200 and json.load(response).get("ok") is True, False
    except (OSError, ValueError, URLError):
        return False, False


class TunnelProbe:
    def __init__(self, opener=urlopen, clock=time.monotonic):
        self.opener = opener
        self.clock = clock
        self.next_public_check = 0.0
        self.public_ok = False

    def __call__(self) -> tuple[bool, bool]:
        try:
            with self.opener("http://127.0.0.1:4040/api/tunnels", timeout=2) as response:
                tunnels = json.load(response).get("tunnels", [])
        except (OSError, ValueError, URLError):
            self.next_public_check = 0
            return False, False
        if not any(item.get("public_url") == TUNNEL_URL for item in tunnels):
            self.next_public_check = 0
            return False, True

        now = self.clock()
        if now >= self.next_public_check:
            request = Request(
                f"{TUNNEL_URL}/healthz",
                headers={"ngrok-skip-browser-warning": "true"},
            )
            try:
                with self.opener(request, timeout=4) as response:
                    self.public_ok = response.status == 200 and json.load(response).get("ok") is True
            except (OSError, ValueError, URLError):
                self.public_ok = False
            self.next_public_check = now + PUBLIC_CHECK_SECONDS
        return self.public_ok, True


tunnel_probe = TunnelProbe()


def process_list() -> list[dict]:
    command = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'ngrok.exe'\" "
        "| Select-Object Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Khong doc duoc danh sach tien trinh Windows")
    if not result.stdout.strip():
        return []
    parsed = json.loads(result.stdout)
    return parsed if isinstance(parsed, list) else [parsed]


def bot_process_exists() -> bool:
    root = str(ROOT).casefold()
    for item in process_list():
        command = (item.get("CommandLine") or "").casefold()
        if (item.get("Name") or "").casefold() in {"python.exe", "pythonw.exe"}:
            if root in command and re.search(r"(?:^|\s|\")bot\.py(?:\s|$|\")", command):
                return True
    return False


def tunnel_process_exists() -> bool:
    for item in process_list():
        command = (item.get("CommandLine") or "").casefold()
        if ((item.get("Name") or "").casefold() == "ngrok.exe"
                and re.search(r"\bhttp\s+8080\b", command)
                and TUNNEL_URL.casefold() in command):
            return True
    return False


def note(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_customer_status() -> None:
    notifier = None
    while True:
        started = time.monotonic()
        try:
            if notifier is None:
                config = load_config()
                store = Store(config.database_path, config.inventory_encryption_key)
                notifier = CustomerStatusNotifier(
                    store, config.bot_token, config.admin_ids,
                    probe=lambda: bot_probe()[0],
                )
            healthy, _ = bot_probe()
            batch = notifier.observe(healthy)
            if batch:
                note(f"Thong bao trang thai {batch['state']} cho {batch['recipients']} khach")
            notifier.send_due()
        except Exception as exc:
            note(f"Khong the gui trang thai cho khach ({type(exc).__name__})")
        time.sleep(max(0, CHECK_SECONDS - (time.monotonic() - started)))


@dataclass
class Service:
    name: str
    command: list[str]
    log_path: Path
    probe: Callable[[], tuple[bool, bool]]
    process_exists: Callable[[], bool]
    alert: Callable[[str], bool] | None = None
    process: subprocess.Popen | None = None
    log_file: object = None
    started_at: float = 0
    retry_at: float = 0
    retry_delay: int = CHECK_SECONDS
    state: str = field(default="", init=False)
    down_since: float | None = None
    outage_notified: bool = False
    next_alert_at: float = 0
    recovery_pending: bool = False
    recovery_retry_at: float = 0

    def announce(self, state: str, message: str) -> None:
        if state != self.state:
            self.state = state
            note(f"{self.name}: {message}")

    def check(self, now: float) -> None:
        healthy = self._check_service(now)
        self._observe_health(now, healthy)

    def _check_service(self, now: float) -> bool:
        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            if self.log_file is not None:
                self.log_file.close()
                self.log_file = None
            self.retry_at = now + self.retry_delay
            self.retry_delay = min(self.retry_delay * 2, MAX_RETRY_SECONDS)
            self.announce("exited", f"da thoat (ma {code}); se thu chay lai")

        try:
            healthy, occupied = self.probe()
        except Exception as exc:
            self.announce("probe_error", f"kiem tra trang thai loi: {exc}")
            return False
        if healthy:
            self.retry_delay = CHECK_SECONDS
            self.retry_at = 0
            self.announce("healthy", "dang chay")
            return True
        if occupied:
            self.announce("tunnel_unhealthy", "ngrok dang chay nhung tunnel khong truy cap duoc")
            return False
        if self.process is not None:
            if now - self.started_at >= STARTUP_SECONDS:
                self.announce("unhealthy", f"tien trinh con chay nhung chua san sang; xem {self.log_path}")
            else:
                self.announce("starting", "dang khoi dong")
            return False

        try:
            if self.process_exists():
                self.announce("existing", "tien trinh co san dang chay; doi dich vu san sang")
                return False
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.announce("inspect_error", f"khong kiem tra duoc tien trinh: {exc}")
            return False

        if now < self.retry_at:
            return False
        try:
            self.log_file = self.log_path.open("ab")
            self.process = subprocess.Popen(
                self.command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=self.log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started_at = now
            self.announce("starting", f"dang khoi dong (PID {self.process.pid})")
        except OSError as exc:
            if self.log_file is not None:
                self.log_file.close()
                self.log_file = None
            self.retry_at = now + self.retry_delay
            self.retry_delay = min(self.retry_delay * 2, MAX_RETRY_SECONDS)
            self.announce("start_error", f"khong chay duoc: {exc}")
        return False

    def _send_alert(self, message: str) -> bool:
        if self.alert is None:
            return False
        try:
            return bool(self.alert(message))
        except Exception as exc:
            note(f"{self.name}: khong gui duoc canh bao ({type(exc).__name__})")
            return False

    def _observe_health(self, now: float, healthy: bool) -> None:
        if healthy:
            if self.down_since is not None:
                self.recovery_pending = self.outage_notified
                self.recovery_retry_at = now
                self.down_since = None
                self.outage_notified = False
                self.next_alert_at = 0
            if self.recovery_pending and now >= self.recovery_retry_at:
                message = f"{self.name} da hoat dong lai. Kiem tra /reconcile neu co giao dich trong luc gian doan."
                if self._send_alert(message):
                    self.recovery_pending = False
                    note(f"{self.name}: da bao admin khi dich vu phuc hoi")
                else:
                    self.recovery_retry_at = now + ALERT_RETRY_SECONDS
            return

        if self.down_since is None:
            self.down_since = now
            self.recovery_pending = False
        if now - self.down_since < OUTAGE_SECONDS or now < self.next_alert_at:
            return
        message = (
            f"Canh bao: {self.name} mat ket noi qua {OUTAGE_SECONDS} giay. "
            "Cua so Giam sat shop dang thu khoi phuc. Kiem tra bot/ngrok va doi soat SePay."
        )
        if self._send_alert(message):
            self.outage_notified = True
            self.next_alert_at = now + ALERT_REPEAT_SECONDS
            note(f"{self.name}: da bao admin ve su co mat ket noi")
        else:
            self.next_alert_at = now + ALERT_RETRY_SECONDS


def main() -> int:
    if not PYTHON.is_file():
        note(f"Khong tim thay Python cua bot: {PYTHON}")
        return 1
    RUNTIME.mkdir(exist_ok=True)
    import msvcrt

    with (RUNTIME / "supervisor.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            note("Cua so giam sat shop da mo. Khong can bam them lan nua.")
            return 0

        services = [
            Service("Bot", [str(PYTHON), "bot.py"], RUNTIME / "bot-supervised.log",
                    bot_probe, bot_process_exists, send_admin_message),
            Service("ngrok", ["ngrok", "http", str(LOCAL_PORT), "--url", TUNNEL_URL],
                    RUNTIME / "ngrok-supervised.log", tunnel_probe, tunnel_process_exists,
                    send_admin_message),
        ]
        threading.Thread(target=run_customer_status, name="customer-status", daemon=True).start()
        note("Dang giam sat bot va ngrok. Giu cua so nay mo de tu khoi dong lai.")
        note("Dong cua so se dung giam sat; bot/ngrok dang chay van tiep tuc chay.")
        try:
            while True:
                now = time.monotonic()
                for service in services:
                    service.check(now)
                time.sleep(CHECK_SECONDS)
        except KeyboardInterrupt:
            note("Da dung giam sat. Bot/ngrok dang chay van tiep tuc chay.")
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
