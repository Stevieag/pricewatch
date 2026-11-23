"""Safe HTTP fetching.

Every outbound request to a *product page* goes through ``safe_get``, which:
  * allows only http/https,
  * refuses hosts that resolve to private / loopback / link-local / reserved
    addresses (SSRF guard - an AI-suggested or shared link can't make the app
    poke your router, NAS or other localhost services),
  * re-checks every redirect hop,
  * caps response size and time,
  * sends no cookies and no credentials.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError(f"Can't resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def check_url_allowed(url: str, allow_private: bool = False) -> None:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"}:
        raise FetchError("Only http(s) URLs can be fetched.")
    if not p.hostname:
        raise FetchError("URL has no host.")
    if p.username or p.password:
        raise FetchError("URLs with embedded credentials are refused.")
    if not allow_private and not _host_is_public(p.hostname):
        raise FetchError(f"Refusing to fetch {p.hostname}: it resolves to a private/local address.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # we follow redirects manually so each hop is re-checked


def safe_get(url: str, *, user_agent: str, timeout: int = 20,
             allow_private: bool = False, accept_language: str = "en-GB,en;q=0.9") -> FetchResult:
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url_allowed(current, allow_private)
        req = urllib.request.Request(current, headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Accept-Language": accept_language,
        })
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urllib.parse.urljoin(current, e.headers["Location"])
                continue
            host = urlsplit(current).hostname
            if e.code in (401, 403, 429, 503):
                raise FetchError(f"{host} blocked the automated request (HTTP {e.code}). "
                                 "Some shops use bot protection; see README > Troubleshooting.") from None
            raise FetchError(f"HTTP {e.code} from {host}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"Network error: {getattr(e, 'reason', e)}") from None
        with resp:
            data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FetchError("Page too large.")
            ctype = resp.headers.get("Content-Type", "")
            charset = resp.headers.get_content_charset() or "utf-8"
            return FetchResult(current, resp.status, ctype, data.decode(charset, errors="replace"))
    raise FetchError("Too many redirects.")



# ---------------------------------------------------------------- real-browser fallback
#
# Some shops (Argos, Currys, Amazon...) block plain HTTP clients by fingerprinting the
# connection itself, whatever User-Agent is sent. In that case PriceWatch runs an
# installed Chromium-family browser (Chrome, Edge, Brave, Chromium) headless, or Firefox
# (see firefox_get below) when none of those is installed:
#   * with a brand-new, throw-away profile -> none of YOUR cookies, logins or history
#     are ever used (this also keeps Fair Price comparisons anonymous),
#   * extensions disabled, no first-run UI, killed after a timeout,
#   * the URL is SSRF-checked first, exactly like safe_get.

import os
import shutil
import subprocess
import tempfile

BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "~/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "brave-browser",
]


def find_browser(configured: str = "") -> str | None:
    """Configured path, else the first Chromium-family browser, else Firefox."""
    for c in ([configured] if configured else []) + BROWSER_CANDIDATES + FIREFOX_CANDIDATES:
        if not c:
            continue
        c = os.path.expanduser(c)
        if os.path.isabs(c):
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


import threading as _threading

BROWSER_SLOTS = _threading.BoundedSemaphore(2)  # at most 2 headless browsers at once


def browser_get(url: str, *, browser: str, user_agent: str, timeout: int = 45,
                allow_private: bool = False, accept_language: str = "en-GB,en;q=0.9",
                override_ua: bool = False) -> FetchResult:
    """override_ua: also make Firefox claim `user_agent` (used by Fair Price personas).
    Otherwise Firefox keeps its own genuine User-Agent, which looks less bot-like."""
    with BROWSER_SLOTS:
        return _browser_get(url, browser=browser, user_agent=user_agent, timeout=timeout,
                            allow_private=allow_private, accept_language=accept_language,
                            override_ua=override_ua)


def _browser_get(url: str, *, browser: str, user_agent: str, timeout: int,
                 allow_private: bool, accept_language: str, override_ua: bool) -> FetchResult:
    if is_firefox(browser):  # Firefox keeps its own genuine User-Agent
        return firefox_get(url, browser=browser, timeout=max(timeout, 60),
                           allow_private=allow_private, accept_language=accept_language,
                           user_agent=user_agent if override_ua else None)
    check_url_allowed(url, allow_private)
    lang = accept_language.split(",")[0]
    with tempfile.TemporaryDirectory(prefix="pricewatch-browser-") as profile:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
               "--no-default-browser-check", "--disable-extensions", "--disable-sync",
               "--disable-background-networking", "--mute-audio", "--hide-scrollbars",
               f"--user-data-dir={profile}", f"--user-agent={user_agent}", f"--lang={lang}",
               "--window-size=1366,900", "--virtual-time-budget=10000", "--dump-dom", url]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.insert(1, "--no-sandbox")  # Chrome refuses to run as root otherwise (containers)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise FetchError("Browser fetch timed out.") from None
        except OSError as e:
            raise FetchError(f"Couldn't start the browser: {e.strerror}") from None
    html = proc.stdout.decode("utf-8", errors="replace")
    if len(html) < 200:
        raise FetchError("Browser fetch returned an empty page.")
    if re.search(r"<title>\s*(Access Denied|Just a moment|Attention Required)", html, re.I):
        raise FetchError("The shop's bot protection blocked the browser fetch too.")
    return FetchResult(url, 200, "text/html", html)


# ---------------------------------------------------------------- Firefox (Marionette)
#
# Firefox has no --dump-dom, so we drive it over Marionette, its built-in automation
# protocol (length-prefixed JSON over a localhost TCP socket). Same guarantees as above:
# throw-away profile, headless, killed afterwards, URL SSRF-checked first.

import json as _json
import socket as _socket
import time as _time

FIREFOX_CANDIDATES = [
    "/Applications/Firefox.app/Contents/MacOS/firefox",
    "~/Applications/Firefox.app/Contents/MacOS/firefox",
    "/Applications/Firefox Developer Edition.app/Contents/MacOS/firefox",
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "firefox", "firefox-esr",
]


def is_firefox(path: str) -> bool:
    return "firefox" in os.path.basename(path).lower()


def _free_port() -> int:
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Marionette:
    def __init__(self, port: int, deadline: float):
        last = None
        while _time.time() < deadline:
            try:
                self.sock = _socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError as e:
                last = e
                _time.sleep(0.25)
        else:
            raise FetchError(f"Firefox automation didn't start ({last}).")
        self.sock.settimeout(max(5, deadline - _time.time()))
        self.buf = b""
        self.msg_id = 0
        self._read()  # hello: {"applicationType": "gecko", "marionetteProtocol": 3}

    def _read(self):
        while b":" not in self.buf:
            self._fill()
        n, _, rest = self.buf.partition(b":")
        n = int(n)
        self.buf = rest
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return _json.loads(data)

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise FetchError("Firefox closed the automation connection.")
        self.buf += chunk

    def cmd(self, name: str, params: dict | None = None):
        self.msg_id += 1
        body = _json.dumps([0, self.msg_id, name, params or {}]).encode()
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        while True:
            msg = self._read()
            if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == self.msg_id:
                if msg[2]:
                    err = msg[2].get("message") or msg[2].get("error") if isinstance(msg[2], dict) else msg[2]
                    raise FetchError(f"Firefox: {str(err)[:200]}")
                return msg[3]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


FIREFOX_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "app.update.enabled": False,
    "app.update.auto": False,
    "extensions.update.enabled": False,
    "browser.sessionstore.resume_from_crash": False,
    "media.autoplay.default": 5,
}


def firefox_get(url: str, *, browser: str, timeout: int = 60, allow_private: bool = False,
                accept_language: str = "en-GB,en;q=0.9", user_agent: str | None = None) -> FetchResult:
    check_url_allowed(url, allow_private)
    deadline = _time.time() + timeout
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="pricewatch-firefox-") as profile:
        prefs = {**FIREFOX_PREFS, "marionette.port": port,
                 "intl.accept_languages": accept_language.split(";")[0].replace(",", ", ")}
        if user_agent:
            prefs["general.useragent.override"] = user_agent
        with open(os.path.join(profile, "user.js"), "w", encoding="utf-8") as f:
            for k, v in prefs.items():
                f.write(f"user_pref({_json.dumps(k)}, {_json.dumps(v)});\n")
        try:
            proc = subprocess.Popen(
                [browser, "--headless", "--marionette", "--no-remote", "--profile", profile],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "MOZ_HEADLESS": "1"})
        except OSError as e:
            raise FetchError(f"Couldn't start Firefox: {e.strerror}") from None
        m = None
        try:
            m = _Marionette(port, deadline)
            m.cmd("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "normal"}}})
            m.cmd("WebDriver:SetTimeouts", {"pageLoad": int(max(5, deadline - _time.time() - 5) * 1000)})
            m.cmd("WebDriver:Navigate", {"url": url})
            _time.sleep(3)  # let client-side scripts render the price
            final = (m.cmd("WebDriver:GetCurrentURL") or {}).get("value") or url
            html = (m.cmd("WebDriver:GetPageSource") or {}).get("value") or ""
            try:
                m.cmd("Marionette:Quit", {"flags": ["eForceQuit"]})
            except FetchError:
                pass
        except (OSError, ValueError) as e:
            raise FetchError(f"Firefox fetch failed ({type(e).__name__}).") from None
        finally:
            if m:
                m.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    if len(html) < 200:
        raise FetchError("Firefox returned an empty page.")
    if re.search(r"<title>\s*(Access Denied|Just a moment|Attention Required)", html, re.I):
        raise FetchError("The shop's bot protection blocked the Firefox fetch too.")
    check_url_allowed(final, allow_private)  # redirects must still land somewhere public
    return FetchResult(final, 200, "text/html", html)
