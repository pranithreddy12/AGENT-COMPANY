"""Rehearse ONE real deal end to end against a LIVE Company OS, over the exact HTTP contract
LeadForge will use. This is the "run one real deal" dress rehearsal: it proves the hardened
proposal loop (async generate -> poll -> human approve -> gated text release) with your real model
before you wire the LeadForge side.

    # 1. start the server in another terminal:  uvicorn app.main:app --port 8000
    # 2. give this script a ceo/dept_head token (or login creds) and run it:
    COMPANY_OS_TOKEN=<paste access_token>  python -m scripts.run_one_deal
    #    or:
    COMPANY_OS_EMAIL=launch@test.com COMPANY_OS_PASSWORD=test123 python -m scripts.run_one_deal

Env:
    COMPANY_OS_URL       base URL (default http://127.0.0.1:8000)
    COMPANY_OS_TOKEN     a ceo/dept_head bearer token (from POST /login or POST /orgs)
    COMPANY_OS_EMAIL/PASSWORD   used to POST /login for a token if COMPANY_OS_TOKEN is unset

The prospect below is the Dubai med-spa — edit PROSPECT to run a different real deal.
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx

# The real deal. Edit this to the prospect you actually want to send to.
PROSPECT = {
    "event": "proposal_requested",
    "company": "Glow Med-Spa Dubai",
    "industry": "med-spa",
    "location": "Dubai",
    "contact_name": "A. Owner",
    "contact_email": "owner@glow.ae",
    "signals": [
        {"signal": "no online booking system", "source": "google places"},
        {"signal": "missed-call complaints in recent reviews", "source": "review scrape"},
        {"signal": "thin opening hours listed", "source": "google places"},
    ],
    "context": "Can you send me a proposal?",
    "leadforge_lead_id": "lf_dubai_001",  # same id on a retry -> same proposal (idempotency)
}


def p(*a):
    print(*a, flush=True)


def _token(base: str, client: httpx.Client) -> str:
    tok = os.environ.get("COMPANY_OS_TOKEN", "").strip()
    if tok:
        return tok
    email, pw = os.environ.get("COMPANY_OS_EMAIL"), os.environ.get("COMPANY_OS_PASSWORD")
    if not (email and pw):
        p("Need a token: set COMPANY_OS_TOKEN, or COMPANY_OS_EMAIL + COMPANY_OS_PASSWORD to log in.")
        sys.exit(2)
    r = client.post(f"{base}/login", json={"email": email, "password": pw})
    r.raise_for_status()
    return r.json()["access_token"]


def main() -> int:
    base = os.environ.get("COMPANY_OS_URL", "http://127.0.0.1:8000").rstrip("/")
    with httpx.Client(timeout=30) as client:
        try:
            token = _token(base, client)
        except httpx.HTTPError as e:
            p(f"Login failed: {e}")
            return 1
        h = {"authorization": f"Bearer {token}"}

        p(f"=== Rehearsing one real deal: {PROSPECT['company']} @ {base} ===\n")

        # 1. LeadForge posts the warm prospect. Returns instantly with an id, NO text.
        p("[1/5] POST /integrations/leadforge/proposal (async kickoff) ...")
        r = client.post(f"{base}/integrations/leadforge/proposal", json=PROSPECT, headers=h)
        r.raise_for_status()
        started = r.json()
        pid = started["proposal_id"]
        assert "proposal" not in started, "webhook must not return the draft text"
        p(f"  -> proposal_id={pid} status={started['status']} idempotent={started['idempotent']}\n")

        # 2. Poll until the background generation finishes (research + LLM + Legal).
        p("[2/5] GET /proposals/{id} — polling while it generates (real model call) ...")
        got = started
        for _ in range(120):  # up to ~2 min for a cloud model
            time.sleep(1)
            got = client.get(f"{base}/proposals/{pid}", headers=h).json()
            if got["status"] != "generating":
                break
        p(f"  -> status={got['status']} ready={got['ready']} blocked={got['blocked']}")
        if got["status"] == "failed":
            p("  generation failed (no model/provider configured?). Check MISTRAL_API_KEY / the org's agents.")
            return 1

        # 3. The gate: before approval, the text is withheld.
        p("\n[3/5] Gate check — text must be withheld until a human approves:")
        p(f"  'proposal' present in response? {'proposal' in got}  (should be False)\n")

        # 4. The human approval step (in production this is a click in the Company OS console).
        p("[4/5] POST /proposals/{id}/approve (the human decision) ...")
        ap = client.post(f"{base}/proposals/{pid}/approve", headers=h)
        if ap.status_code != 200:
            p(f"  approve returned {ap.status_code}: {ap.text}")
            p("  (409 = still generating or Legal-blocked; a blocked proposal must clear the veto first)")
            return 1
        p(f"  -> {ap.json()}\n")

        # 5. Now the text is released — this is exactly what LeadForge would fetch and send.
        p("[5/5] GET /proposals/{id} — approved, text released:")
        final = client.get(f"{base}/proposals/{pid}", headers=h).json()
        p(f"  status={final['status']} ready={final['ready']}\n")
        p("=" * 70)
        p(final.get("proposal", "(no text)").strip())
        p("=" * 70)
        p("\nThis is the send-ready proposal. In production LeadForge fetches this same text and sends it.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
