"""CEO console: an exception dashboard, not a chat window. Standup digest + a thin HTML screen."""
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Principal, current_principal
from app.db import get_db
from app.models import ApprovalRequest, Artifact, Organization, Project, Task, Thread
from app.services import governance, scheduling

router = APIRouter(tags=["console"])


@router.get("/console/standup")
def standup(db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> dict:
    org = db.get(Organization, p.org_id)
    projects = list(db.scalars(select(Project).where(Project.org_id == p.org_id)))
    tasks = list(db.scalars(select(Task).where(Task.org_id == p.org_id)))

    # at-risk = a scheduled/running task whose remaining effort leaves no buffer (naive: slack < effort*0.2)
    at_risk = [t for t in tasks if t.status in ("scheduled", "running") and scheduling.slip_risk(t.slack_h, t.est_effort_hours, buffer=0.2)]

    pending = db.scalar(select(func.count(ApprovalRequest.id)).where(
        ApprovalRequest.org_id == p.org_id, ApprovalRequest.status == "pending")) or 0
    needs_human = db.scalar(select(func.count(Artifact.id)).where(
        Artifact.org_id == p.org_id, Artifact.needs_human == True)) or 0  # noqa: E712
    blocked_arts = db.scalar(select(func.count(Artifact.id)).where(
        Artifact.org_id == p.org_id, Artifact.blocked == True)) or 0  # noqa: E712
    escalated = db.scalar(select(func.count(Thread.id)).where(
        Thread.org_id == p.org_id, Thread.status == "escalated")) or 0

    return {
        "shipped": {
            "projects_done": sum(1 for x in projects if x.status == "done"),
            "tasks_done": sum(1 for t in tasks if t.status == "done"),
        },
        "at_risk": {
            "projects_slipping": sum(1 for x in projects if x.health == "slipping"),
            "tasks_at_risk": len(at_risk),
            "tasks_blocked": sum(1 for t in tasks if t.status == "blocked"),
        },
        "needs_you": {
            "pending_approvals": pending,
            "artifacts_need_human": needs_human,
            "artifacts_blocked_by_legal": blocked_arts,
            "escalated_threads": escalated,
        },
        "cost": {
            "spent_usd": round(governance.spent(db, p.org_id), 4),
            "cap_usd": org.cost_cap_usd,
            "remaining_usd": round(governance.remaining_budget(db, org), 4),
        },
        "controls": {"killed": org.killed, "simulation": org.simulation},
    }


@router.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    # thin single-screen console over the JSON APIs. ponytail: vanilla HTML, no build chain;
    # swap for Next.js/shadcn when the console needs to grow.
    return _HTML


_HTML = """<!doctype html><meta charset=utf-8><title>Company OS — CEO Console</title>
<style>
 body{font:14px system-ui;margin:0;background:#0b0e14;color:#d7dce5}
 header{padding:12px 20px;background:#11151f;border-bottom:1px solid #222}
 main{padding:20px;max-width:900px;margin:auto;display:grid;gap:16px}
 .card{background:#131824;border:1px solid #222c3d;border-radius:10px;padding:16px}
 h2{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:#8b96a8}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
 .kpi{background:#0e1420;border:1px solid #202a3a;border-radius:8px;padding:10px}
 .kpi b{display:block;font-size:22px}
 input,button{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid #2a3550;background:#0e1420;color:#d7dce5}
 button{background:#2b6cff;border:0;cursor:pointer}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .appr{border-top:1px solid #202a3a;padding:8px 0}
 small{color:#7b8698}
</style>
<header class=row><b>Company OS</b> — CEO Console
 <span style=flex:1></span>
 <input id=tok placeholder="paste access_token" size=40>
 <button onclick=loadAll()>Load</button></header>
<main>
 <div class=card><h2>Directive</h2><div class=row>
   <input id=goal placeholder="State a goal…" style=flex:1>
   <button onclick=directive()>Plan it</button></div>
   <small id=dmsg></small></div>
 <div class=card><h2>Standup digest</h2><div id=kpis class=grid></div></div>
 <div class=card><h2>Decision queue</h2><div id=appr></div></div>
 <div class=card><h2>Org controls</h2><div class=row>
   <button onclick=post('/governance/kill')>Kill switch</button>
   <button onclick=post('/governance/resume')>Resume</button>
   <button onclick=post('/governance/simulation?on=true')>Simulation on</button></div></div>
</main>
<script>
const T=()=>document.getElementById('tok').value
const H=()=>({authorization:'Bearer '+T(),'content-type':'application/json'})
async function loadAll(){await standup();await approvals()}
async function standup(){
 const s=await(await fetch('/console/standup',{headers:H()})).json()
 const k=document.getElementById('kpis');k.innerHTML=''
 const rows=[['Projects done',s.shipped.projects_done],['Tasks done',s.shipped.tasks_done],
  ['Slipping',s.at_risk.projects_slipping],['At risk',s.at_risk.tasks_at_risk],
  ['Pending approvals',s.needs_you.pending_approvals],['Need human',s.needs_you.artifacts_need_human],
  ['Blocked (legal)',s.needs_you.artifacts_blocked_by_legal],['Spent $',s.cost.spent_usd]]
 for(const [l,v] of rows)k.insertAdjacentHTML('beforeend',`<div class=kpi><small>${l}</small><b>${v}</b></div>`)
}
async function approvals(){
 const a=await(await fetch('/approvals',{headers:H()})).json()
 const el=document.getElementById('appr')
 el.innerHTML=a.length?'':'<small>Nothing awaiting you.</small>'
 for(const x of a)el.insertAdjacentHTML('beforeend',
  `<div class=appr><div>${x.action_type}: <b>${x.preview}</b></div><div class=row>
   <input placeholder=reason id=r_${x.id}>
   <button onclick="decide('${x.id}','approve')">Approve</button>
   <button onclick="decide('${x.id}','reject')">Reject</button></div></div>`)
}
async function decide(id,d){
 const reason=(document.getElementById('r_'+id)||{}).value||null
 await fetch('/approvals/'+id+'/decide',{method:'POST',headers:H(),body:JSON.stringify({decision:d,reason})})
 approvals();standup()
}
async function directive(){
 const goal=document.getElementById('goal').value
 const r=await fetch('/projects',{method:'POST',headers:H(),body:JSON.stringify({goal})})
 document.getElementById('dmsg').textContent=r.ok?'Plan drafted — review tasks via API, then approve.':'Error'
 standup()
}
async function post(u){await fetch(u,{method:'POST',headers:H()});standup()}
</script>"""
