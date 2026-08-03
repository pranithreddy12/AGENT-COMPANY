import pytest

from app.models import Actor, Organization
from app.tenancy import CrossTenantError, Tenant


def _org(db, name):
    o = Organization(name=name)
    db.add(o)
    db.flush()
    return o


def test_cross_tenant_read_returns_nothing(db):
    a, b = _org(db, "A"), _org(db, "B")
    actor = Actor(org_id=a.id, type="agent")
    db.add(actor)
    db.commit()

    assert Tenant(db, a.id).get(Actor, actor.id) is not None
    assert Tenant(db, b.id).get(Actor, actor.id) is None  # invisible across tenants
    assert Tenant(db, b.id).all(Actor) == []


def test_cross_tenant_write_raises(db):
    a, b = _org(db, "A"), _org(db, "B")
    stray = Actor(org_id=a.id, type="agent")
    with pytest.raises(CrossTenantError):
        Tenant(db, b.id).add(stray)  # refuses to write another org's row
