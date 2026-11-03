"""Dashboard login: one username and two passwords, both required.

Only salted PBKDF2 hashes are stored, in .env (set them with `python analysis/set_dashboard_password.py`).
Failed logins are counted per client IP; MAX_FAILS within LOCKOUT locks that IP until LOCKOUT after the last one.
After a login the browser keeps a signed cookie for SESSION_DAYS, so page reloads, dropped connections and
server restarts don't ask again. The signature key comes from the password hashes, so changing the
passwords signs everyone out.
"""
import hashlib
import json
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_KEY, PASS1_KEY, PASS2_KEY = "DASHBOARD_USER", "DASHBOARD_PASS1_HASH", "DASHBOARD_PASS2_HASH"
ITERATIONS = 310_000
MAX_FAILS, LOCKOUT = 5, 15 * 60                          # 5 wrong tries in 15 min -> locked 15 min
PUBLIC_NAME = "Library Management"                     # what the login page and browser tab show
FAIL_DELAY = 1.5                                         # seconds added to every failed attempt
SESSION_DAYS = 7                                         # stay logged in this long without re-entering
COOKIE = "lm_session"


def hash_password(password: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(calc.hex(), digest)


def credentials():
    """(user, hash1, hash2) from the environment / .env, or None if login is not set up."""
    from market_agent.config import load_env
    load_env(PROJECT_ROOT / ".env")
    vals = tuple(os.environ.get(k, "") for k in (USER_KEY, PASS1_KEY, PASS2_KEY))
    return vals if all(vals) else None


@st.cache_resource
def _failures():
    """Shared by every browser session of this server: {ip: [failure times]}."""
    return {"lock": threading.Lock(), "by_ip": {}}


def _locked_for(ip: str) -> float:
    """Seconds this IP must still wait (0 = may try)."""
    f = _failures()
    with f["lock"]:
        now = time.time()
        recent = [t for t in f["by_ip"].get(ip, []) if now - t < LOCKOUT]
        f["by_ip"][ip] = recent
        return recent[-1] + LOCKOUT - now if len(recent) >= MAX_FAILS else 0.0


def _record(ip: str, ok: bool):
    f = _failures()
    with f["lock"]:
        if ok:
            f["by_ip"].pop(ip, None)
        else:
            f["by_ip"].setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- remember-me cookie
def _sign(creds, payload: str) -> str:
    key = hashlib.sha256(("session|" + creds[1] + "|" + creds[2]).encode()).digest()
    return hmac.new(key, f"{creds[0]}|{payload}".encode(), hashlib.sha256).hexdigest()


def make_token(creds, now: float = None) -> str:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(creds, str(expires))}"


def token_valid(token, creds, now: float = None) -> bool:
    if not isinstance(token, str) or token.count(".") != 1:
        return False
    expires, sig = token.split(".")
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(creds, expires))


def _set_cookie(value: str, max_age: int):
    """Writes the cookie from the browser (Streamlit can't set response headers)."""
    js = json.dumps(f"{COOKIE}={value}; path=/; max-age={max_age}; SameSite=Strict")
    st.html(f"<script>document.cookie = {js} + (location.protocol === 'https:' ? '; Secure' : '');</script>",
            unsafe_allow_javascript=True)


def require_login():
    """Shows a neutral login form and stops the page until the user is logged in.
    Without credentials in .env the dashboard stays open, unless MDA_PUBLIC=1 (then it refuses to run)."""
    creds = credentials()
    # a valid cookie from an earlier login counts as logged in (ignored right after "Log out" in this tab,
    # because the cookie the server saw when the tab connected can't change until the next connection)
    if (creds and not st.session_state.get("auth_user") and not st.session_state.get("logged_out")
            and token_valid(st.context.cookies.get(COOKIE), creds)):
        st.session_state["auth_user"] = creds[0]
    if st.session_state.get("auth_user"):
        if st.session_state.pop("new_login", False):
            _set_cookie(make_token(creds), SESSION_DAYS * 86400)
        if st.sidebar.button("Log out"):
            st.session_state.pop("auth_user")
            st.session_state["logged_out"] = True
            st.rerun()
        return
    if st.session_state.get("logged_out"):
        _set_cookie("", 0)                  # delete it in the browser
    if creds is None:
        if os.environ.get("MDA_PUBLIC") == "1":
            print("Login is not set up. Run: .venv/bin/python analysis/set_dashboard_password.py", flush=True)
            st.error("Sign-in is not available.")
            st.stop()
        return

    ip = st.context.ip_address
    ip = ip if isinstance(ip, str) and ip else "local"      # None when browsing from the server itself
    # the page is public, so it shows a neutral name, not what is behind it
    st.title(PUBLIC_NAME)
    st.caption("Sign in to continue")
    wait = _locked_for(ip)
    if wait:
        st.error(f"Too many failed attempts. Try again in {int(wait // 60) + 1} min.")
        st.stop()
    with st.form("login"):
        user = st.text_input("Username", autocomplete="username")
        pw1 = st.text_input("Password 1", type="password")
        pw2 = st.text_input("Password 2", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        # check everything every time, so the response doesn't reveal which part was wrong
        ok = [hmac.compare_digest(user.strip().encode(), creds[0].encode()),
              verify_password(pw1, creds[1]), verify_password(pw2, creds[2])]
        _record(ip, all(ok))
        if all(ok):
            st.session_state["auth_user"] = creds[0]
            st.session_state["new_login"] = True       # write the cookie on the next run
            st.session_state.pop("logged_out", None)
            st.rerun()
        time.sleep(FAIL_DELAY)
        st.error("Wrong username or passwords.")
    st.stop()
