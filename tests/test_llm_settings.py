"""Org-level LLM 'brain' settings: pick a provider/model/key from the console instead of .env."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import settings as app_settings
from app.db import Base, get_db
from app.main import app
from app.models import AgentProfile, Organization
from app.routers.orgs import create_org
from app.schemas import OrgCreate
from app.services import llm


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def _org(db):
    return create_org(OrgCreate(name="Acme", ceo_email="c@a.com", ceo_password="pw"), db).org_id


def test_resolve_api_key_falls_back_to_env_when_org_has_none(db, monkeypatch):
    org_id = _org(db)
    monkeypatch.setattr(app_settings, "mistral_api_key", "env-key-123")
    assert llm.resolve_api_key(db, org_id, "mistral") == "env-key-123"


def test_resolve_api_key_prefers_org_configured_key(db, monkeypatch):
    org_id = _org(db)
    monkeypatch.setattr(app_settings, "mistral_api_key", "env-key-123")
    org = db.get(Organization, org_id)
    org.llm_provider, org.llm_api_keys = "mistral", {"mistral": "org-key-456"}
    db.commit()
    assert llm.resolve_api_key(db, org_id, "mistral") == "org-key-456"


def test_resolve_api_key_ignores_org_key_configured_for_a_different_provider(db, monkeypatch):
    org_id = _org(db)
    monkeypatch.setattr(app_settings, "mistral_api_key", "env-key-123")
    org = db.get(Organization, org_id)
    org.llm_provider, org.llm_api_keys = "openrouter", {"openrouter": "or-key-789"}  # a DIFFERENT provider's key
    db.commit()
    assert llm.resolve_api_key(db, org_id, "mistral") == "env-key-123"  # must not leak the openrouter key


def test_resolve_api_key_keeps_each_providers_key_independent_across_switches(db, monkeypatch):
    """The exact regression this design guards against: configure mistral, switch to openrouter with
    a new key, switch BACK to mistral with no new key — mistral's own key must still be there, not
    silently replaced by openrouter's."""
    org_id = _org(db)
    monkeypatch.setattr(app_settings, "mistral_api_key", None)  # force reliance on the org-stored key
    llm.configure_org_llm(db, org_id, "mistral", "mistral-small-latest", "mistral-key-1")
    llm.configure_org_llm(db, org_id, "openrouter", "some/model", "openrouter-key-2")
    db.commit()
    assert llm.resolve_api_key(db, org_id, "openrouter") == "openrouter-key-2"
    llm.configure_org_llm(db, org_id, "mistral", "mistral-small-latest", None)  # switch back, no new key
    db.commit()
    assert llm.resolve_api_key(db, org_id, "mistral") == "mistral-key-1"  # untouched by the openrouter save
    assert llm.resolve_api_key(db, org_id, "openrouter") == "openrouter-key-2"  # also still intact


def test_resolve_api_key_needs_no_key_for_echo_or_ollama(db):
    org_id = _org(db)
    assert llm.resolve_api_key(db, org_id, "echo") is None
    assert llm.resolve_api_key(db, org_id, "ollama") is None


def test_configure_org_llm_updates_org_and_every_agent_profile(db):
    org_id = _org(db)
    llm.configure_org_llm(db, org_id, "openrouter", "mistralai/mistral-small-3.2-24b-instruct:free", "sk-or-abc")
    db.commit()
    org = db.get(Organization, org_id)
    assert org.llm_provider == "openrouter"
    assert org.llm_model == "mistralai/mistral-small-3.2-24b-instruct:free"
    assert org.llm_api_keys == {"openrouter": "sk-or-abc"}
    profiles = list(db.scalars(select(AgentProfile).where(AgentProfile.org_id == org_id)))
    assert profiles and all(p.provider == "openrouter" and p.model == org.llm_model for p in profiles)


def test_configure_org_llm_blank_key_keeps_the_existing_one(db):
    org_id = _org(db)
    llm.configure_org_llm(db, org_id, "mistral", "mistral-small-latest", "first-key")
    llm.configure_org_llm(db, org_id, "mistral", "mistral-large-latest", None)  # model change, no new key
    db.commit()
    org = db.get(Organization, org_id)
    assert org.llm_model == "mistral-large-latest"
    assert org.llm_api_keys == {"mistral": "first-key"}  # untouched


def test_build_provider_openrouter_requires_a_key():
    with pytest.raises(RuntimeError, match="openrouter"):
        llm.build_provider("openrouter", "some/model", None)


def test_build_provider_openrouter_with_a_key_constructs_cleanly():
    p = llm.build_provider("openrouter", "mistralai/mistral-small-3.2-24b-instruct:free", "sk-or-abc")
    assert isinstance(p, llm.OpenRouterProvider)
    assert p.model == "mistralai/mistral-small-3.2-24b-instruct:free"
    assert p.base_url == app_settings.openrouter_base_url.rstrip("/")


def test_build_provider_openrouter_registers_the_model_so_cost_compute_never_fails_closed():
    """Reproduces the real failure: cost.compute() runs AFTER a successful completion and raises
    UnknownModelError for any model not in the static RATES table — which every OpenRouter model is,
    since the catalog is open-ended. That silently turned a successful run into a reported failure
    (the agent's actual reply was discarded; the chat showed 'model call: <model-name>', the str() of
    the raised exception). build_provider must register the model so this can never happen again."""
    from app.services import cost

    model = "test/openrouter-arbitrary-model-never-priced"
    assert model not in cost.RATES
    llm.build_provider("openrouter", model, "sk-or-abc")
    assert cost.compute(model, 100, 50) == 0.0  # would have raised UnknownModelError before the fix


@pytest.mark.parametrize("attr", ["ollama_base_url", "mistral_base_url", "openrouter_base_url"])
def test_openai_compatible_base_urls_dont_already_end_in_v1(attr):
    """OllamaProvider._chat always appends '/v1/chat/completions' itself — a base URL that already
    ends in '/v1' (an easy mistake, since that IS the real API host for some providers) doubles it
    into '.../v1/v1/chat/completions' and 404s. Regression guard for exactly that bug."""
    url = getattr(app_settings, attr)
    assert not url.rstrip("/").endswith("/v1"), f"{attr}={url!r} already ends in /v1 — the client will double it"


def test_openrouter_provider_builds_the_real_completions_url(monkeypatch):
    """End-to-end proof, not just a string check: point a real OpenRouterProvider at a fake local
    server and confirm the URL it actually POSTs to is the correct, non-doubled path."""
    import httpx

    seen = {}

    def fake_post(url, json, timeout, headers):
        seen["url"] = url
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = llm.OpenRouterProvider(api_key="sk-or-abc", model="some/model", base_url=app_settings.openrouter_base_url)
    p.complete(system="", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=16)
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"


def _client_with(db):
    def _override():
        yield db
    app.dependency_overrides[get_db] = _override
    return TestClient(app)


def test_llm_settings_http_roundtrip_never_returns_the_raw_key(db):
    client = _client_with(db)
    try:
        org_id = _org(db)
        db.commit()
        r = create_org(OrgCreate(name="Http Co", ceo_email="ceo2@a.com", ceo_password="pw"), db)
        db.commit()
        tok = r.access_token

        got = client.get("/settings/llm", headers={"authorization": f"Bearer {tok}"})
        assert got.status_code == 200
        assert got.json()["provider"] is None and got.json()["has_key"] is False

        posted = client.post("/settings/llm", headers={"authorization": f"Bearer {tok}"},
                             json={"provider": "openrouter", "model": "mistralai/mistral-small-3.2-24b-instruct:free",
                                   "api_key": "sk-or-super-secret"})
        assert posted.status_code == 200
        assert "api_key" not in posted.text and "sk-or-super-secret" not in posted.text

        got2 = client.get("/settings/llm", headers={"authorization": f"Bearer {tok}"})
        body = got2.json()
        assert body["provider"] == "openrouter" and body["has_key"] is True
        assert "sk-or-super-secret" not in got2.text  # the raw key never comes back over the wire
    finally:
        app.dependency_overrides.clear()


def test_llm_settings_rejects_unknown_provider(db):
    client = _client_with(db)
    try:
        r = create_org(OrgCreate(name="Bad Co", ceo_email="ceo3@a.com", ceo_password="pw"), db)
        db.commit()
        resp = client.post("/settings/llm", headers={"authorization": f"Bearer {r.access_token}"},
                           json={"provider": "chatgpt-turbo-9000", "model": "x"})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_llm_settings_rejects_keyed_provider_with_no_key_anywhere(db, monkeypatch):
    monkeypatch.setattr(app_settings, "openrouter_api_key", None)
    client = _client_with(db)
    try:
        r = create_org(OrgCreate(name="No Key Co", ceo_email="ceo4@a.com", ceo_password="pw"), db)
        db.commit()
        resp = client.post("/settings/llm", headers={"authorization": f"Bearer {r.access_token}"},
                           json={"provider": "openrouter", "model": "some/model"})  # no api_key given, no env fallback
        assert resp.status_code == 422
        # and nothing got persisted from the failed attempt
        got = client.get("/settings/llm", headers={"authorization": f"Bearer {r.access_token}"})
        assert got.json()["provider"] is None
    finally:
        app.dependency_overrides.clear()


def test_llm_settings_requires_ceo_role(db):
    """A dept_head (or any non-ceo) must not be able to repoint every agent's provider/key."""
    client = _client_with(db)
    try:
        r = create_org(OrgCreate(name="Role Co", ceo_email="ceo5@a.com", ceo_password="pw"), db)
        db.commit()
        ceo_h = {"authorization": f"Bearer {r.access_token}"}
        dept_id = client.get("/departments", headers=ceo_h).json()[0]["id"]
        client.post("/team", headers=ceo_h,
                    json={"email": "member@a.com", "password": "member-pw",
                          "department_id": dept_id, "role": "dept_head"})
        member_tok = client.post("/login", json={"email": "member@a.com", "password": "member-pw"}).json()["access_token"]
        resp = client.post("/settings/llm", headers={"authorization": f"Bearer {member_tok}"},
                           json={"provider": "echo", "model": "echo-1"})
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
