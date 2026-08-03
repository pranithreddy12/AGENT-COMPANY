"""Shared query layer. Tenant isolation enforced here, not by convention.

Every read/write goes through a Tenant bound to one org_id. A row from another org is
invisible; adding a row for another org raises. Tested in test_tenancy.py.
"""
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base

T = TypeVar("T", bound=Base)


class CrossTenantError(Exception):
    pass


class Tenant:
    def __init__(self, session: Session, org_id: str):
        self.session = session
        self.org_id = org_id

    def all(self, model: type[T]) -> list[T]:
        return list(self.session.scalars(select(model).where(model.org_id == self.org_id)))

    def get(self, model: type[T], id: str) -> T | None:
        return self.session.scalars(
            select(model).where(model.id == id, model.org_id == self.org_id)
        ).first()

    def add(self, obj: T) -> T:
        if getattr(obj, "org_id", None) != self.org_id:
            raise CrossTenantError(
                f"refusing to write {type(obj).__name__} for org {getattr(obj, 'org_id', None)!r} "
                f"through tenant {self.org_id!r}"
            )
        self.session.add(obj)
        return obj

    def commit(self) -> None:
        self.session.commit()
