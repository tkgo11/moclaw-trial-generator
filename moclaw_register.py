#!/usr/bin/env python3
"""
moclaw_register.py
──────────────────
Automates MoClaw free-trial registration using a disposable inbox from mail.gw.

Single run:
  python3 moclaw_register.py
  python3 moclaw_register.py --headed        # show the browser
  python3 moclaw_register.py --inbox         # dump inbox after login

Bulk run (parallel workers):
  python3 moclaw_register.py --bulk 5        # register 5 accounts
  python3 moclaw_register.py --bulk 10 --workers 3   # 10 accounts, 3 at a time
  python3 moclaw_register.py --bulk 5 --output accounts.json

All credentials are appended to moclaw_credentials.json (or --output file).
"""

import argparse
import json
import os
import queue
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# ─── Constants ────────────────────────────────────────────────────────────────
MOCLAW_AUTH_URL     = "https://moclaw.ai/auth"
MOCLAW_PRICING_URL  = "https://moclaw.ai/pricing"
MAILGW_BASE         = "https://api.mail.gw"
POLL_INTERVAL       = 5     # seconds between inbox polls
POLL_TIMEOUT        = 90    # max seconds to wait for OTP email
DEFAULT_WORKERS     = 2     # default parallel workers for bulk mode

# ─── Thread-safe print lock ───────────────────────────────────────────────────
_print_lock = threading.Lock()

def log(msg: str, emoji: str = "•", prefix: str = "") -> None:
    with _print_lock:
        tag = f"[{prefix}] " if prefix else "  "
        print(f"{tag}{emoji}  {msg}", flush=True)


# ─── Browser helpers (per-session profile) ────────────────────────────────────

def _browser_flags(session_name: str) -> list[str]:
    profile = f".camoufox-{session_name}"
    # --session isolates this browser instance from all others in parallel
    return ["--session", session_name, "--persistent", profile, "--locale", "en-US"]


def _run_browser(*args: str, session_name: str, headed: bool = False) -> str:
    cmd = ["camoufox-cli"]
    if headed:
        cmd += ["--headed"]
    cmd += _browser_flags(session_name)
    cmd += list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and result.stderr.strip():
        raise RuntimeError(f"camoufox-cli error: {result.stderr.strip()}")
    return result.stdout.strip()


def _clean_profile(session_name: str) -> None:
    profile = f".camoufox-{session_name}"
    if os.path.exists(profile):
        shutil.rmtree(profile, ignore_errors=True)


# ─── Step 1: Create Temporary Email ───────────────────────────────────────────

def create_temp_email(prefix: str = "") -> dict:
    """Create a disposable inbox on mail.gw."""
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    })

    r = s.get(f"{MAILGW_BASE}/domains", timeout=10)
    r.raise_for_status()
    domains = [d["domain"] for d in r.json()["hydra:member"] if d.get("isActive", True)]
    domain = random.choice(domains)

    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=16))
    email    = f"{username}@{domain}"

    log(f"Creating inbox: {email}", "📬", prefix=prefix)
    r = s.post(f"{MAILGW_BASE}/accounts", json={"address": email, "password": password}, timeout=10)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create mail.gw account: {r.status_code} {r.text}")

    r = s.post(f"{MAILGW_BASE}/token", json={"address": email, "password": password}, timeout=10)
    r.raise_for_status()
    token = r.json()["token"]

    return {"email": email, "password": password, "token": token, "session": s}


# ─── Step 2: Poll Inbox for OTP ───────────────────────────────────────────────

def _extract_otp(text: str) -> Optional[str]:
    matches = re.findall(r"\b(\d{6})\b", text)
    return matches[0] if matches else None


def wait_for_otp(token: str, session: requests.Session, prefix: str = "") -> str:
    """Poll mail.gw inbox until OTP arrives."""
    log(f"Waiting up to {POLL_TIMEOUT}s for OTP …", "⏳", prefix=prefix)
    auth_header = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        r = session.get(f"{MAILGW_BASE}/messages", headers=auth_header, timeout=10)
        r.raise_for_status()
        for msg in r.json()["hydra:member"]:
            r2 = session.get(f"{MAILGW_BASE}/messages/{msg['id']}", headers=auth_header, timeout=10)
            r2.raise_for_status()
            full    = r2.json()
            combined = (full.get("subject") or "") + " " + (full.get("text") or "")
            otp = _extract_otp(combined)
            if otp:
                log(f"OTP received: {otp}", "✅", prefix=prefix)
                return otp

        remaining = int(deadline - time.time())
        log(f"No OTP yet, retrying in {POLL_INTERVAL}s ({remaining}s left) …", "🔄", prefix=prefix)
        time.sleep(POLL_INTERVAL)

    raise TimeoutError("OTP email did not arrive within the timeout period.")


# ─── Step 3: Browser Automation ───────────────────────────────────────────────

def _parse_refs(snap: str) -> dict:
    """Extract useful element refs from a snapshot string."""
    refs = {}
    for line in snap.splitlines():
        m = re.search(r"\[ref=(e\d+)\]", line)
        if not m:
            continue
        ref = m.group(1)
        ll = line.lower()
        if "textbox" in ll:
            if "email" in ll or "enter your email" in ll:
                refs["email_input"] = ref
            elif "code" in ll or "otp" in ll or "enter code" in ll or "verification" in ll:
                refs["otp_input"] = ref
            else:
                # Generic textbox — could be either; store as fallback
                refs.setdefault("generic_textbox", ref)
        elif "button" in ll and "continue" in ll and "email" in ll:
            refs["submit"] = ref
    return refs


def register_one(session_name: str, headed: bool = False, prefix: str = "") -> dict:
    """Full registration flow for a single account. Returns credential dict."""

    _clean_profile(session_name)

    # ── Create temp email ──
    creds = create_temp_email(prefix=prefix)
    email, token, mail_session = creds["email"], creds["token"], creds["session"]

    # ── Open MoClaw auth ──
    log("Opening MoClaw auth …", "🌐", prefix=prefix)
    _run_browser("open", MOCLAW_AUTH_URL, session_name=session_name, headed=headed)
    time.sleep(4)  # allow JS to hydrate
    snap = _run_browser("snapshot", "-i", session_name=session_name, headed=headed)
    # If snapshot is empty or too short, wait and retry once
    if len(snap.strip()) < 20:
        time.sleep(4)
        snap = _run_browser("snapshot", "-i", session_name=session_name, headed=headed)

    refs = _parse_refs(snap)
    if "email_input" not in refs:
        # fallback: first textbox
        for line in snap.splitlines():
            if "textbox" in line.lower():
                m = re.search(r"\[ref=(e\d+)\]", line)
                if m:
                    refs["email_input"] = m.group(1)
                    break
    if "email_input" not in refs:
        raise RuntimeError(f"Email input not found. Snapshot:\n{snap}")

    log(f"Submitting: {email}", "✍️", prefix=prefix)
    _run_browser("fill", f"@{refs['email_input']}", email, session_name=session_name, headed=headed)
    time.sleep(0.4)
    if "submit" in refs:
        _run_browser("click", f"@{refs['submit']}", session_name=session_name, headed=headed)
    else:
        _run_browser("press", "Enter", session_name=session_name, headed=headed)
    time.sleep(3)

    # ── Wait for OTP ──
    otp = wait_for_otp(token, mail_session, prefix=prefix)

    # ── Enter OTP ──
    snap2 = _run_browser("snapshot", "-i", session_name=session_name, headed=headed)
    refs2 = _parse_refs(snap2)
    # Fallback: if no dedicated otp_input, use the only textbox on screen
    if "otp_input" not in refs2 and "generic_textbox" in refs2:
        refs2["otp_input"] = refs2["generic_textbox"]
    if "otp_input" not in refs2:
        raise RuntimeError(f"OTP input not found. Snapshot:\n{snap2}")

    log(f"Entering OTP: {otp}", "🔢", prefix=prefix)
    _run_browser("fill", f"@{refs2['otp_input']}", otp, session_name=session_name, headed=headed)
    _run_browser("press", "Enter", session_name=session_name, headed=headed)

    # Wait for redirect to /chat — poll up to 15s
    final_url = ""
    for _ in range(15):
        time.sleep(1)
        final_url = _run_browser("url", session_name=session_name, headed=headed)
        if "/chat" in final_url or "/dashboard" in final_url:
            break

    title = _run_browser("title", session_name=session_name, headed=headed)

    # ── Activate trial ──
    trial_activated = False
    if "/chat" in final_url or "/dashboard" in final_url:
        trial_activated = activate_trial(session_name=session_name, headed=headed, prefix=prefix)

    # Close browser for this session (bulk mode reuses no browser)
    _run_browser("close", session_name=session_name, headed=headed)
    _clean_profile(session_name)

    return {
        "email":             email,
        "mailgw_password":   creds["password"],
        "mailgw_token":      token,
        "moclaw_url":        final_url,
        "page_title":        title,
        "registered_at":     datetime.now(timezone.utc).isoformat(),
        "success":           "/chat" in final_url,
        "trial_activated":   trial_activated,
    }


# ─── Step 4: Activate Trial ───────────────────────────────────────────────────

def activate_trial(session_name: str, headed: bool = False, prefix: str = "") -> bool:
    """Navigate to /pricing and click 'Start free trial'. Returns True on success."""
    log("Navigating to pricing page to activate trial …", "💳", prefix=prefix)
    _run_browser("open", MOCLAW_PRICING_URL, session_name=session_name, headed=headed)
    time.sleep(3)  # allow page to fully render

    snap = _run_browser("snapshot", "-i", session_name=session_name, headed=headed)

    # Find the first "Start free trial" button ref
    trial_ref = None
    for line in snap.splitlines():
        ll = line.lower()
        if "start free trial" in ll or ("start" in ll and "trial" in ll):
            m = re.search(r"\[ref=(e\d+)\]", line)
            if m:
                trial_ref = m.group(1)
                break

    if not trial_ref:
        log("'Start free trial' button not found on pricing page — may already be subscribed.", "⚠️", prefix=prefix)
        return False

    log(f"Clicking 'Start free trial' (ref={trial_ref}) …", "🖱️", prefix=prefix)
    _run_browser("click", f"@{trial_ref}", session_name=session_name, headed=headed)

    # Wait for redirect / confirmation (checkout page or dashboard)
    final_url = ""
    for _ in range(20):
        time.sleep(1)
        final_url = _run_browser("url", session_name=session_name, headed=headed)
        if any(kw in final_url for kw in ("/checkout", "/subscribe", "/billing", "/chat", "/dashboard", "stripe", "paddle")):
            break

    log(f"Trial activation landed on: {final_url}", "🎯", prefix=prefix)
    return True


# ─── Step 5: View Inbox ───────────────────────────────────────────────────────

def print_inbox(token: str, session: requests.Session) -> None:
    auth_header = {"Authorization": f"Bearer {token}"}
    r = session.get(f"{MAILGW_BASE}/messages", headers=auth_header, timeout=10)
    messages = r.json()["hydra:member"]

    print("\n" + "─" * 60)
    print(f"  📥  INBOX  ({len(messages)} message(s))")
    print("─" * 60)
    if not messages:
        print("  (empty)")
    for i, msg in enumerate(messages, 1):
        r2 = session.get(f"{MAILGW_BASE}/messages/{msg['id']}", headers=auth_header, timeout=10)
        full = r2.json()
        print(f"\n  [{i}] From:    {full.get('from', {}).get('address', '?')}")
        print(f"      Subject: {full.get('subject', '(no subject)')}")
        print(f"      Date:    {full.get('createdAt', '?')}")
        body = (full.get("text") or full.get("intro") or "").strip()
        if body:
            preview = body[:400].replace("\n", " ")
            print(f"      Body:    {preview}{'…' if len(body) > 400 else ''}")
    print("─" * 60)


# ─── Single registration entry point ─────────────────────────────────────────

def run_single(args: argparse.Namespace) -> None:
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║   MoClaw Free Trial — Auto Registration Script   ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    result = register_one(session_name="moclaw-single", headed=args.headed, prefix="")

    email  = result["email"]
    pw     = result["mailgw_password"]
    url    = result["moclaw_url"]
    title  = result["page_title"]
    trial  = "✅ Yes" if result.get("trial_activated") else "⚠️  No"

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║              ✅  REGISTRATION COMPLETE           ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Email   : {email:<38} ║")
    print(f"║  Password: {pw:<38} ║")
    print(f"║  URL     : {url[:38]:<38} ║")
    print(f"║  Title   : {title[:38]:<38} ║")
    print(f"║  Trial   : {trial:<38} ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    out_file = args.output or "moclaw_credentials.json"
    _append_credential(result, out_file)
    log(f"Credentials saved → {out_file}", "💾")
    print()
    print("  📌  Temp inbox expires in ~10 min — save credentials now!")
    print()

    if args.inbox:
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        print_inbox(result["mailgw_token"], s)


# ─── Bulk registration entry point ───────────────────────────────────────────

def _append_credential(cred: dict, path: str) -> None:
    """Thread-safely append a credential dict to a JSON array file."""
    with _print_lock:
        existing = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = [existing]
            except Exception:
                existing = []
        existing.append(cred)
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)


def _worker(job_q: queue.Queue, results: list, out_file: str,
            headed: bool, lock: threading.Lock) -> None:
    while True:
        try:
            idx = job_q.get_nowait()
        except queue.Empty:
            break

        prefix = f"#{idx:02d}"
        session_name = f"moclaw-bulk-{idx}"
        try:
            result = register_one(session_name=session_name, headed=headed, prefix=prefix)
            result["index"] = idx
            _append_credential(result, out_file)
            with lock:
                results.append(result)
            status = "✅ success" if result["success"] else "⚠️  landed on unexpected URL"
            log(f"{result['email']}  →  {status}", "🎉", prefix=prefix)
        except Exception as e:
            err = {"index": idx, "error": str(e), "success": False,
                   "registered_at": datetime.now(timezone.utc).isoformat()}
            _append_credential(err, out_file)
            with lock:
                results.append(err)
            log(f"Failed: {e}", "❌", prefix=prefix)
        finally:
            job_q.task_done()


def run_bulk(args: argparse.Namespace) -> None:
    count    = args.bulk
    workers  = min(args.workers, count)
    out_file = args.output or "moclaw_credentials.json"

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║   MoClaw Bulk Registration — {count} accounts, {workers} parallel worker(s)   ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    log(f"Output file: {out_file}", "📁")
    log(f"Starting {count} registrations with {workers} concurrent worker(s) …", "🚀")
    print()

    job_q   = queue.Queue()
    results = []
    lock    = threading.Lock()

    for i in range(1, count + 1):
        job_q.put(i)

    threads = []
    for _ in range(workers):
        t = threading.Thread(
            target=_worker,
            args=(job_q, results, out_file, args.headed, lock),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Progress ticker
    total = count
    while not job_q.empty() or any(t.is_alive() for t in threads):
        done = sum(1 for r in results)
        with _print_lock:
            print(f"\r  ⏳  Progress: {done}/{total} complete …", end="", flush=True)
        time.sleep(1)

    for t in threads:
        t.join()

    print(f"\r  ✅  Done: {len(results)}/{total} processed.           ")
    print()

    # Summary table
    success = [r for r in results if r.get("success")]
    failed  = [r for r in results if not r.get("success")]
    trials  = [r for r in results if r.get("trial_activated")]

    print("─" * 74)
    print(f"  {'#':<4}  {'Email':<40}  {'Registered':<12}  {'Trial'}")
    print("─" * 74)
    for r in sorted(results, key=lambda x: x.get("index", 0)):
        idx    = r.get("index", "?")
        email  = r.get("email", "—")
        ok     = "✅ OK" if r.get("success") else f"❌ {r.get('error', 'failed')[:15]}"
        trial  = "✅ Yes" if r.get("trial_activated") else "⚠️  No"
        print(f"  {idx:<4}  {email:<40}  {ok:<12}  {trial}")
    print("─" * 74)
    print(f"  Total: {len(results)}  |  Registered: {len(success)}  |  Trial activated: {len(trials)}  |  Failed: {len(failed)}")
    print(f"  Saved to: {out_file}")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MoClaw free-trial auto-registration (single or bulk)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 moclaw_register.py                        # single account
  python3 moclaw_register.py --headed --inbox       # single, show browser + inbox
  python3 moclaw_register.py --bulk 5               # 5 accounts, 2 workers (default)
  python3 moclaw_register.py --bulk 10 --workers 4  # 10 accounts, 4 parallel
  python3 moclaw_register.py --bulk 3 --output accounts.json
        """,
    )
    parser.add_argument("--headed",  action="store_true", help="Show the browser window")
    parser.add_argument("--inbox",   action="store_true", help="Dump inbox after single registration")
    parser.add_argument("--bulk",    type=int, default=0, metavar="N",
                        help="Register N accounts in bulk (default: 0 = single)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, metavar="W",
                        help=f"Parallel workers for bulk mode (default: {DEFAULT_WORKERS})")
    parser.add_argument("--output",  type=str, default=None, metavar="FILE",
                        help="Output JSON file (default: moclaw_credentials.json)")
    args = parser.parse_args()

    try:
        if args.bulk > 0:
            run_bulk(args)
        else:
            run_single(args)
    except KeyboardInterrupt:
        print("\n\n  Aborted.")
        sys.exit(0)
    except Exception as e:
        print(f"\n  ❌  Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
