"""Read-only Authenticated Web Trading Terminal Server for NQ + XAUUSD MT5 Bot."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from btc_15m_research import BtcResearchService
from market_feed import MarketFeedService
from terminal_data import ROOT_DIR, TerminalDataService

WEB_DIR = ROOT_DIR / "web"
PASSCODE_FILE = ROOT_DIR / ".terminal_passcode"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("terminal_server")

STATIC_ROUTES: Dict[str, Tuple[Path, str]] = {
    "/": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
    "/index.html": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
    "/styles.css": (WEB_DIR / "styles.css", "text/css; charset=utf-8"),
    "/app.js": (WEB_DIR / "app.js", "application/javascript; charset=utf-8"),
    "/tv_chart.js": (WEB_DIR / "tv_chart.js", "application/javascript; charset=utf-8"),
    "/vendor/lightweight-charts.js": (WEB_DIR / "vendor" / "lightweight-charts.js", "application/javascript; charset=utf-8"),
    "/manifest.json": (WEB_DIR / "manifest.json", "application/manifest+json; charset=utf-8"),
    "/sw.js": (WEB_DIR / "sw.js", "application/javascript; charset=utf-8"),
    "/icon.svg": (WEB_DIR / "icon.svg", "image/svg+xml; charset=utf-8"),
    "/icon-192.png": (WEB_DIR / "icon-192.png", "image/png"),
    "/icon-512.png": (WEB_DIR / "icon-512.png", "image/png"),
    "/apple-touch-icon.png": (WEB_DIR / "apple-touch-icon.png", "image/png"),
}

SESSION_COOKIE_NAME = "trading_terminal_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60


def resolve_passcode(cli_passcode: Optional[str] = None, passcode_file: Path = PASSCODE_FILE) -> str:
    """Return configured passcode or persist a generated one for restarts."""
    if cli_passcode:
        return cli_passcode.strip()
    env_code = os.environ.get("TRADING_TERMINAL_PASSCODE", "").strip()
    if env_code:
        return env_code
    if passcode_file.exists():
        saved = passcode_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    generated = secrets.token_urlsafe(8)
    passcode_file.write_text(generated, encoding="utf-8")
    return generated


def discover_local_urls(port: int) -> Dict[str, List[str]]:
    """Discover loopback, local Wi-Fi LAN, and Tailscale IPv4 URLs for convenience."""
    lan_ips: List[str] = []
    tailscale_ips: List[str] = []
    seen = set()

    # Probe preferred outbound interface without sending network packets
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            primary = s.getsockname()[0]
            if primary and primary != "127.0.0.1":
                seen.add(primary)
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip_str = info[4][0]
            if ip_str and ip_str != "127.0.0.1":
                seen.add(ip_str)
    except OSError:
        pass

    ts_net = ipaddress.ip_network("100.64.0.0/10")
    for ip_str in sorted(seen):
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if addr in ts_net:
            tailscale_ips.append(f"http://{ip_str}:{port}")
        elif addr.is_private and not addr.is_link_local:
            lan_ips.append(f"http://{ip_str}:{port}")

    return {
        "local": [f"http://127.0.0.1:{port}"],
        "lan": lan_ips,
        "tailscale": tailscale_ips,
    }


class AuthStore:
    """In-memory session and failed-login rate limiter."""

    def __init__(self, passcode: Optional[str], auth_required: bool = True, public_showcase: bool = False) -> None:
        self.passcode = passcode or ""
        self.auth_required = auth_required
        self.public_showcase = public_showcase
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}
        self._failures: Dict[str, List[float]] = {}

    def verify_session(self, token: Optional[str]) -> bool:
        if not self.auth_required:
            return True
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if now > exp:
                del self._sessions[token]
                return False
            self._sessions[token] = now + SESSION_TTL_SECONDS
            return True

    def try_login(self, candidate: str, client_ip: str) -> Tuple[bool, Optional[str], Optional[int]]:
        """Verify `candidate` passcode. Returns `(ok, session_token, retry_after_sec)`."""
        if not self.auth_required:
            return True, "", None
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client_ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
            self._failures[client_ip] = recent
            if len(recent) >= MAX_LOGIN_FAILURES:
                retry = int(max(1, LOGIN_LOCKOUT_SECONDS - (now - recent[0])))
                return False, None, retry

            matched = hmac.compare_digest(candidate.encode("utf-8"), self.passcode.encode("utf-8"))
            if not matched:
                recent.append(now)
                self._failures[client_ip] = recent
                return False, None, None

            self._failures.pop(client_ip, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + SESSION_TTL_SECONDS
            return True, token, None

    def logout(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)


class TerminalRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler serving static assets and authenticated read-only MT5 APIs."""

    server_version = "TradeTerminal/1.0"

    @property
    def app_service(self) -> TerminalDataService:
        return self.server.data_service  # type: ignore[attr-defined]

    @property
    def market_feed(self) -> MarketFeedService:
        return self.server.market_feed  # type: ignore[attr-defined]

    @property
    def auth_store(self) -> AuthStore:
        return self.server.auth_store  # type: ignore[attr-defined]

    @property
    def btc_research(self) -> BtcResearchService:
        return self.server.btc_research  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep console noise clean except explicit errors/info
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            log.warning("%s - %s", self.address_string(), fmt % args)

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _extract_session_token(self) -> Optional[str]:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel is not None else None

    def _reject_cross_site(self) -> bool:
        site = self.headers.get("Sec-Fetch-Site")
        if not site:
            return False
        if site in ("same-origin", "same-site", "none"):
            return False
        mode = self.headers.get("Sec-Fetch-Mode", "")
        dest = self.headers.get("Sec-Fetch-Dest", "")
        if site == "cross-site" and self.command == "GET" and mode == "navigate" and dest not in ("object", "embed"):
            return False
        self._send_json(403, {"error": "Cross-site request blocked"})
        return True

    def _apply_security_headers(self, is_api: bool = False) -> None:
        csp = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self'",
                "img-src 'self' data:",
                "connect-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "frame-ancestors 'none'",
                "form-action 'self'",
            ]
        )
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()")
        self.send_header("Vary", "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site")
        if is_api:
            self.send_header("Cache-Control", "no-store")

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._apply_security_headers(is_api=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(
        self,
        file_path: Path,
        content_type: str,
        extra_headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        if not file_path.exists():
            self._send_json(404, {"error": "Asset not found"})
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self._apply_security_headers(is_api=False)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self) -> bool:
        token = self._extract_session_token()
        if self.auth_store.verify_session(token):
            return True
        self._send_json(401, {"error": "Authentication required"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_cross_site():
            return

        parsed = urlparse(self.path)
        route = parsed.path

        if route in STATIC_ROUTES:
            file_path, ctype = STATIC_ROUTES[route]
            extra = [("Service-Worker-Allowed", "/")] if route == "/sw.js" else None
            self._send_static(file_path, ctype, extra_headers=extra)
            return

        if route == "/api/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "auth_required": self.auth_store.auth_required,
                    "public_showcase": self.auth_store.public_showcase,
                },
            )
            return

        if route == "/api/connection-info":
            if not self._require_auth():
                return
            port = getattr(self.server, "server_port", 8080)
            if self.auth_store.public_showcase:
                self._send_json(
                    200,
                    {
                        "port": port,
                        "auth_required": False,
                        "public_showcase": True,
                        "urls": {"local": [], "lan": [], "tailscale": []},
                    },
                )
                return
            urls = discover_local_urls(port)
            self._send_json(
                200,
                {
                    "port": port,
                    "auth_required": self.auth_store.auth_required,
                    "public_showcase": False,
                    "urls": urls,
                },
            )
            return

        if route == "/api/auth":
            token = self._extract_session_token()
            self._send_json(
                200,
                {
                    "authenticated": self.auth_store.verify_session(token),
                    "auth_required": self.auth_store.auth_required,
                    "public_showcase": self.auth_store.public_showcase,
                },
            )
            return

        if route == "/api/snapshot":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            scope = qs.get("scope", ["all"])[0]
            if scope not in ("all", "bot"):
                scope = "all"
            force = qs.get("force", ["0"])[0] == "1"
            self._send_json(200, self.app_service.get_snapshot(scope=scope, force=force))
            return

        if route == "/api/logs":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            try:
                limit = max(20, min(500, int(qs.get("limit", ["150"])[0])))
            except ValueError:
                limit = 150
            self._send_json(200, {"lines": self.app_service.get_logs(limit=limit)})
            return

        if route == "/api/reports":
            if not self._require_auth():
                return
            self._send_json(200, {"strategies": self.app_service.get_reports()})
            return

        if route == "/api/chart":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            sym = qs.get("symbol", ["USTEC"])[0].strip().upper()[:24]
            tf = qs.get("tf", ["1h"])[0].strip().lower()
            try:
                bars = max(40, min(600, int(qs.get("bars", ["360"])[0])))
            except ValueError:
                bars = 360
            self._send_json(200, self.app_service.get_chart(symbol=sym, timeframe=tf, bars=bars))
            return

        if route == "/api/tv/watchlist":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            extra_raw = qs.get("extra", [""])[0]
            extra_syms = [s.strip().upper()[:24] for s in extra_raw.split(",") if s.strip()][:20]
            force = qs.get("force", ["0"])[0] == "1"
            self._send_json(200, self.market_feed.get_watchlist(extra_symbols=extra_syms, force=force))
            return

        if route == "/api/tv/chart":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            sym = qs.get("symbol", ["NAS100"])[0].strip().upper()[:24]
            tf = qs.get("tf", ["1h"])[0].strip().lower()
            try:
                bars = max(60, min(1000, int(qs.get("bars", ["600"])[0])))
            except ValueError:
                bars = 600
            self._send_json(200, self.market_feed.get_chart(symbol=sym, timeframe=tf, bars=bars))
            return

        if route == "/api/tv/btc-predict":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query)
            force = qs.get("force", ["0"])[0] == "1"
            ivs_raw = qs.get("intervals", ["5m,15m"])[0]
            ivs = [s.strip().lower() for s in ivs_raw.split(",") if s.strip() in ("5m", "15m")]
            self._send_json(200, self.btc_research.get_predictions(intervals=ivs or ("5m", "15m"), force=force))
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_cross_site():
            return

        parsed = urlparse(self.path)
        route = parsed.path
        content_len = int(self.headers.get("Content-Length", "0") or 0)
        if content_len > 16_384:
            self._send_json(413, {"error": "Payload too large"})
            return
        raw_body = self.rfile.read(content_len) if content_len > 0 else b""

        if route == "/api/login":
            if self.auth_store.public_showcase:
                self._send_json(200, {"authenticated": True, "public_showcase": True})
                return
            passcode = ""
            ctype = self.headers.get("Content-Type", "").lower()
            if "application/json" in ctype and raw_body:
                try:
                    data = json.loads(raw_body.decode("utf-8"))
                    passcode = str(data.get("passcode", ""))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"error": "Invalid JSON"})
                    return
            elif "application/x-www-form-urlencoded" in ctype and raw_body:
                form = parse_qs(raw_body.decode("utf-8", errors="replace"))
                passcode = form.get("passcode", [""])[0]

            client_ip = self.client_address[0]
            ok, token, retry_after = self.auth_store.try_login(passcode, client_ip)
            if not ok:
                if retry_after is not None:
                    self._send_json(
                        429,
                        {"error": f"Too many attempts. Try again in {retry_after}s."},
                        extra_headers=[("Retry-After", str(retry_after))],
                    )
                else:
                    self._send_json(401, {"error": "Invalid passcode"})
                return

            cookie_parts = [
                f"{SESSION_COOKIE_NAME}={token}",
                "HttpOnly",
                "SameSite=Lax",
                "Path=/",
                f"Max-Age={SESSION_TTL_SECONDS}",
            ]
            if self._is_https():
                cookie_parts.append("Secure")
            self._send_json(200, {"authenticated": True}, extra_headers=[("Set-Cookie", "; ".join(cookie_parts))])
            return

        if route == "/api/logout":
            self.auth_store.logout(self._extract_session_token())
            cookie_parts = [
                f"{SESSION_COOKIE_NAME}=",
                "HttpOnly",
                "SameSite=Lax",
                "Path=/",
                "Max-Age=0",
            ]
            headers = [("Set-Cookie", "; ".join(cookie_parts))]
            if self._is_https():
                headers.append(("Clear-Site-Data", '"cookies", "storage", "cache"'))
            self._send_json(200, {"authenticated": False}, extra_headers=headers)
            return

        self._send_json(404, {"error": "Not found"})


def create_terminal_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    passcode: Optional[str] = None,
    no_auth: bool = False,
    public_showcase: bool = False,
    symbols: Optional[List[str]] = None,
    history_days: int = 180,
    server_offset_hours: int = 7,
) -> Tuple[ThreadingHTTPServer, str]:
    """Create the configured `ThreadingHTTPServer` and return `(server, effective_passcode)`."""
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    if no_auth and not public_showcase and not is_loopback:
        raise SystemExit("Safety block: --no-auth is only allowed with --host 127.0.0.1")

    auth_required = not (no_auth or public_showcase)
    effective_passcode = "" if not auth_required else resolve_passcode(passcode)
    service = TerminalDataService(
        symbols=symbols or ["NQ", "XAUUSD"],
        history_days=history_days,
        server_offset_hours=server_offset_hours,
        public_showcase=public_showcase,
    )
    market_feed = MarketFeedService(server_offset_hours=server_offset_hours)
    btc_research = BtcResearchService()
    auth_store = AuthStore(
        passcode=effective_passcode,
        auth_required=auth_required,
        public_showcase=public_showcase,
    )
    server = ThreadingHTTPServer((host, port), TerminalRequestHandler)
    server.data_service = service  # type: ignore[attr-defined]
    server.market_feed = market_feed  # type: ignore[attr-defined]
    server.btc_research = btc_research  # type: ignore[attr-defined]
    server.auth_store = auth_store  # type: ignore[attr-defined]
    return server, effective_passcode


def main() -> None:
    env_port_raw = os.environ.get("PORT", "").strip()
    default_port = int(env_port_raw) if env_port_raw.isdigit() else 8080
    env_showcase = os.environ.get("PUBLIC_SHOWCASE", "").strip().lower() in ("1", "true", "yes", "on")

    parser = argparse.ArgumentParser(description="Read-only Web Trading Terminal for NQ + XAUUSD MT5 Bot")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (0.0.0.0 for phone/Tailscale/Wi-Fi, 127.0.0.1 for PC only)")
    parser.add_argument("--port", type=int, default=default_port, help="HTTP port (default $PORT or 8080)")
    parser.add_argument("--passcode", default=None, help="Override login passcode (otherwise saved in .terminal_passcode)")
    parser.add_argument("--no-auth", action="store_true", help="Disable passcode login (only permitted on 127.0.0.1)")
    parser.add_argument(
        "--public-showcase",
        action="store_true",
        default=env_showcase,
        help="Run sanitized public TradingView Chart Studio + Strategy Arena showcase without passcode",
    )
    parser.add_argument("--symbols", nargs="+", default=["NQ", "XAUUSD"])
    parser.add_argument("--history-days", type=int, default=180, help="Lookback days of MT5 closed deal history")
    parser.add_argument("--server-offset-hours", type=int, default=7, help="Broker server clock offset ahead of NY (MetaQuotes-Demo=7)")
    args = parser.parse_args()

    server, effective_passcode = create_terminal_server(
        host=args.host,
        port=args.port,
        passcode=args.passcode,
        no_auth=args.no_auth,
        public_showcase=args.public_showcase,
        symbols=args.symbols,
        history_days=args.history_days,
        server_offset_hours=args.server_offset_hours,
    )

    if args.public_showcase:
        log.info("=== Public Showcase Terminal Ready (MT5 Telemetry Hidden) ===")
        log.info("  Bind       : http://%s:%d", args.host, args.port)
        log.info("  Auth       : PUBLIC SHOWCASE (Chart Studio + 4Y Strategy Arena)")
    else:
        urls = discover_local_urls(args.port)
        log.info("=== Read-Only Trading Terminal Ready ===")
        log.info("  PC Browser : %s", ", ".join(urls["local"]))
        if args.host == "0.0.0.0":
            if urls["lan"]:
                log.info("  Home Wi-Fi : %s", ", ".join(urls["lan"]))
            if urls["tailscale"]:
                log.info("  Tailscale  : %s", ", ".join(urls["tailscale"]))
            else:
                log.info("  Tailscale  : (not detected yet — install Tailscale on PC & phone to use http://<tailscale-ip>:%d anywhere)", args.port)
        if args.no_auth:
            log.info("  Auth       : DISABLED (loopback only)")
        else:
            log.info("  Passcode   : %s (saved in %s)", effective_passcode, PASSCODE_FILE.name)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down terminal server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
