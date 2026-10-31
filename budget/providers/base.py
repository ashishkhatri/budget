"""Interface for future bank-API connectors (e.g. Plaid, SimpleFIN, Teller).

A connector only has to fetch transactions and return them as ``Candidate``
objects with a stable ``external_id``. ``sync_account`` then feeds them
through the same pipeline as file imports, so categorization, rules and
duplicate handling stay identical. Re-syncing an overlapping date range is
safe because (account_id, external_id) is unique in the database.

Sketch of a connector:

    class SimpleFinProvider(Provider):
        name = "simplefin"
        def list_accounts(self): ...
        def fetch_transactions(self, external_account_id, since): ...
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from ..parsing.common import Candidate


@dataclass
class RemoteAccount:
    external_id: str
    name: str
    institution: Optional[str] = None
    type: str = "other"  # checking | savings | credit | other


class Provider(ABC):
    name: str = "provider"

    @abstractmethod
    def list_accounts(self) -> List[RemoteAccount]:
        ...

    @abstractmethod
    def fetch_transactions(self, external_account_id: str, since: Optional[str] = None) -> List[Candidate]:
        """Return transactions dated on/after ``since`` (YYYY-MM-DD). Amounts: negative = money out."""
        ...


def sync_account(con, provider: Provider, remote: RemoteAccount, since: Optional[str] = None) -> dict:
    """Pull one remote account's transactions into the database."""
    from .. import pipeline

    row = con.execute("SELECT id FROM accounts WHERE source = ? AND external_id = ?",
                      (provider.name, remote.external_id)).fetchone()
    if row:
        account_id = row[0]
    else:
        account_id = pipeline.get_or_create_account(con, remote.name, remote.institution, remote.type,
                                                    source=provider.name, external_id=remote.external_id)
    cands = provider.fetch_transactions(remote.external_id, since)
    rows = [dict(date=c.date, description=c.description, amount_cents=c.amount_cents, bank_category=c.bank_category,
                 bank_type=c.bank_type, external_id=c.external_id, pending=c.pending) for c in cands]
    return pipeline.ingest(con, account_id, rows, source=provider.name)
