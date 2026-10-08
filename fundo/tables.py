"""What gets replicated, and how. SOLUTION.md explains each choice."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    name: str
    pk: str
    strategy: str      # "change_tracking": net changes incl. deletes | "append_only": rowversion watermark
    bucket: int        # key range per reconciliation bucket in the checks
    exclude: tuple = ()  # source columns that are not replicated
    changed_at: str = ""  # append_only: the row's timestamp, to age unloaded rows in the checks


TABLES = [
    Table("Customers", "CustomerId", "change_tracking", 500, exclude=("Ssn",)),  # PII the warehouse doesn't need
    Table("Advances", "AdvanceId", "change_tracking", 1000),
    Table("Cards", "CardId", "change_tracking", 500),
    Table("Transactions", "TransactionId", "change_tracking", 5000),
    Table("AdvanceStatusHistory", "HistoryId", "append_only", 5000, exclude=("RowVer",), changed_at="ChangedAt"),
]
BY_NAME = {t.name: t for t in TABLES}

# Source tables we deliberately leave behind. Anything not listed here or in TABLES
# is reported by the checks, so a new table cannot go unnoticed.
NOT_REPLICATED = {
    "tmp_CustomerExport_2021": "scratch export from 2021: no owner, no key, no readers",
}
