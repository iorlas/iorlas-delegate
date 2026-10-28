"""CostLedger: what this tool spent on the credit lane, from its ledger rows."""
from delegate.contracts.history import LedgerRow


class CostLedger:
    def __init__(self, rows: list[LedgerRow]) -> None:
        self.rows = rows

    def spend(self, since: float) -> float:
        return sum(r.cost for r in self.rows if r.ts >= since)

    def last_ts(self) -> float | None:
        return self.rows[-1].ts if self.rows else None
