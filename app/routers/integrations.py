"""Inbound integrations. LeadForge posts here when a prospect is ready for delivery."""
import html as _html
import secrets as pysecrets
import threading

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import Principal, hash_secret, leadforge_principal, require_role
from app.db import get_db
from app.models import Organization
from app.routers.projects import _task_out
from app.schemas import LeadForgeHandoff, LeadForgeHandoffResult
from app.services import integrations

router = APIRouter(tags=["integrations"])


def _share_url(request: Request, token: str | None) -> str | None:
    return str(request.base_url).rstrip("/") + f"/p/{token}" if token else None


def _accept_page(token: str, project, art, acc, *, error: str | None = None) -> str:
    """Minimal self-served client page: the approved proposal + an accept form (or, once signed, the
    signed banner). Everything client-supplied is HTML-escaped."""
    text = _html.escape(art.content or "")
    title = _html.escape(project.goal or "Proposal")
    if acc is not None:
        top = (f'<div class="ok">Accepted by {_html.escape(acc.signer_name)} on '
               f'{acc.accepted_at:%Y-%m-%d %H:%M} UTC</div>')
        form = ""
    else:
        err = f'<div class="err">{_html.escape(error)}</div>' if error else ""
        top = ""
        form = (f'{err}<form method="post" action="/p/{_html.escape(token)}/accept" class="accept">'
                '<label for="signer_name">Type your full name to accept this proposal:</label>'
                '<input id="signer_name" name="signer_name" required autocomplete="name" placeholder="Your full name">'
                '<button type="submit">Accept proposal</button>'
                '<p class="fine">Clicking Accept records your name, the date, and your IP as your '
                'agreement to this proposal.</p></form>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Proposal — {title}</title><style>'
        'body{font-family:Georgia,serif;max-width:760px;margin:2rem auto;padding:0 1rem;'
        'color:#1a2332;background:#fdfcf8}'
        '.doc{white-space:pre-wrap;line-height:1.6;background:#fff;border:1px solid #e6e1d5;'
        'padding:1.5rem;border-radius:6px}'
        '.ok{background:#eef7ee;border:1px solid #bcdcbc;padding:.75rem 1rem;border-radius:6px;margin-bottom:1rem}'
        '.err{background:#fbeaea;border:1px solid #e0b4b4;padding:.6rem 1rem;border-radius:6px;margin-bottom:.75rem}'
        '.accept{margin-top:1.5rem;display:flex;flex-direction:column;gap:.5rem}'
        'input{padding:.6rem;font-size:1rem;border:1px solid #cfc8b8;border-radius:4px}'
        'button{padding:.7rem 1.2rem;font-size:1rem;background:#1a2332;color:#f5e9c8;border:none;'
        'border-radius:4px;cursor:pointer}'
        '.fine{color:#8a8577;font-size:.8rem}</style></head>'
        f'<body>{top}<div class="doc">{text}</div>{form}</body></html>'
    )


@router.post("/integrations/leadforge/secret")
def rotate_secret(db: Session = Depends(get_db), p: Principal = Depends(require_role("ceo"))) -> dict:
    """Generate (or rotate) the long-lived LeadForge webhook secret. Shown once; only its hash
    is stored. Put it in LeadForge as X-LeadForge-Secret."""
    org = db.get(Organization, p.org_id)
    raw = pysecrets.token_urlsafe(32)
    org.webhook_secret_hash = hash_secret(raw)
    db.commit()
    return {"secret": raw, "note": "store in LeadForge as X-LeadForge-Secret; shown once, not recoverable"}


@router.post("/integrations/leadforge/handoff", response_model=LeadForgeHandoffResult)
def leadforge_handoff(body: LeadForgeHandoff, db: Session = Depends(get_db),
                      p: Principal = Depends(leadforge_principal)) -> LeadForgeHandoffResult:
    """LeadForge -> Company OS: a warm reply / proposal request becomes an Account + a decomposed
    delivery Project. Auth: X-LeadForge-Secret (long-lived) or a ceo/dept_head Bearer token."""
    account, lead, project, tasks = integrations.ingest_handoff(db, p.org_id, body)
    db.commit()
    return LeadForgeHandoffResult(
        account_id=account.id, lead_id=lead.id, project_id=project.id,
        project_status=project.status, tasks=[_task_out(t) for t in tasks],
    )


@router.post("/integrations/leadforge/proposal")
def leadforge_proposal(body: LeadForgeHandoff, db: Session = Depends(get_db),
                       p: Principal = Depends(leadforge_principal)) -> dict:
    """Kick off ONE client-ready proposal for a prospect and return immediately with a proposal_id.
    Generation (research + LLM + Legal, up to ~60s) runs in the background; the webhook never blocks
    and never returns the draft text. LeadForge fetches the text later via GET /proposals/{id}, which
    only releases it once a human has approved it. Idempotent on leadforge_lead_id (a retry returns
    the same proposal_id, not a new proposal)."""
    project, is_new = integrations.start_proposal(db, p.org_id, body)
    project_id, status = project.id, project.status
    db.commit()  # persist the shell before the worker (its own session) loads it
    if is_new:
        threading.Thread(target=integrations.run_proposal_in_background,
                         args=(project_id, body), daemon=True).start()
    return {"proposal_id": project_id, "status": status, "idempotent": not is_new}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, request: Request, db: Session = Depends(get_db),
                 p: Principal = Depends(leadforge_principal)) -> dict:
    """Fetch a proposal by id (LeadForge via secret, or a ceo/dept_head via Bearer). Returns status
    only while generating / awaiting approval; releases the proposal TEXT only once a human has
    approved it and it isn't Legal-blocked. Also reports the client share_url and whether the client
    has accepted (the conversion signal)."""
    view = integrations.proposal_view(db, p.org_id, proposal_id)
    if view is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    view["share_url"] = _share_url(request, view.get("share_token"))
    return view


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, request: Request, db: Session = Depends(get_db),
                     p: Principal = Depends(require_role("ceo", "dept_head"))) -> dict:
    """Human-only: approve a generated proposal, mint its client share link, and return share_url —
    the link to send the prospect to view + accept. No webhook-secret path (a machine can't
    self-approve). Refuses a proposal still generating (409) or Legal-blocked (409; override first)."""
    result = integrations.approve_proposal(db, p.org_id, proposal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if result.get("error") == "not_ready":
        raise HTTPException(status_code=409, detail="proposal not generated yet")
    if result.get("error") == "blocked":
        raise HTTPException(status_code=409, detail=f"Legal veto in place — override it first: {result.get('block_reason')}")
    db.commit()
    result["share_url"] = _share_url(request, result.get("accept_token"))
    return result


# ponytail: public endpoints are unauthenticated by design (the client has no account) — the 256-bit
# token IS the gate, one proposal per token, no enumeration. Rate-limiting deferred; add a limiter
# here if these ever face abuse.
@router.get("/p/{token}", response_class=HTMLResponse)
def public_proposal_page(token: str, db: Session = Depends(get_db)) -> HTMLResponse:
    """Public client view: the approved proposal + an accept form, at the shareable /p/{token} link.
    404 if the token is unknown or the proposal isn't approved (unapproved proposals never get a token,
    so their text can't leak here)."""
    pv = integrations.public_proposal(db, token)
    if pv is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    project, art, acc = pv
    return HTMLResponse(_accept_page(token, project, art, acc))


@router.post("/p/{token}/accept")
def public_proposal_accept(token: str, request: Request, signer_name: str = Form(""),
                           db: Session = Depends(get_db)):
    """Client accepts (signs) the proposal. Records name + time + IP + a hash of the exact text, flips
    the deal to 'accepted', and redirects back to the (now signed) page. Idempotent."""
    ip = request.client.host if request.client else None
    result = integrations.accept_proposal_by_token(db, token, signer_name, ip)
    if result is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if result.get("error") == "empty_name":
        pv = integrations.public_proposal(db, token)
        project, art, acc = pv
        return HTMLResponse(_accept_page(token, project, art, acc, error="Please type your name to accept."),
                            status_code=400)
    db.commit()
    return RedirectResponse(url=f"/p/{token}", status_code=303)  # PRG: refresh-safe, shows signed page
