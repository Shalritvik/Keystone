"""Fixture data for the mock servicing console.

Entirely synthetic. No real names, no real account numbers, no real
institutions. The member numbers are deliberately outside any real-world
format and the balances are round.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    kind: str
    balance: str
    status: str = "Open"


@dataclass
class Member:
    member_no: str
    name: str
    status: str
    branch: str
    joined: str
    accounts: list[Account] = field(default_factory=list)
    restricted: bool = False
    """Triggers a permission-denied path, exercising a real authorisation state."""


MEMBERS: dict[str, Member] = {
    "12345": Member(
        member_no="12345",
        name="AVERY R. LINDQUIST",
        status="Active",
        branch="003 - RIVERSIDE",
        joined="04/17/2009",
        accounts=[
            Account("12345-S01", "REGULAR SAVINGS", "4,182.55"),
            Account("12345-S07", "VACATION CLUB", "930.00"),
            Account("12345-D01", "FREE CHECKING", "1,247.83"),
        ],
    ),
    "22881": Member(
        member_no="22881",
        name="MARISOL T. OKAFOR",
        status="Active",
        branch="001 - MAIN OFFICE",
        joined="11/02/2016",
        accounts=[
            Account("22881-S01", "REGULAR SAVINGS", "17,640.12"),
            Account("22881-L02", "AUTO LOAN", "-8,415.00", status="Current"),
        ],
    ),
    "30014": Member(
        member_no="30014",
        name="DESMOND F. HALE",
        status="Dormant",
        branch="005 - NORTHGATE",
        joined="06/28/1998",
        accounts=[
            Account("30014-S01", "REGULAR SAVINGS", "62.40", status="Dormant"),
        ],
    ),
    "44120": Member(
        member_no="44120",
        name="[RESTRICTED RECORD]",
        status="Restricted",
        branch="—",
        joined="—",
        restricted=True,
        accounts=[],
    ),
}

SUB_ACCOUNT_TYPES = [
    ("S02", "SECONDARY SAVINGS"),
    ("S07", "VACATION CLUB"),
    ("S09", "HOLIDAY CLUB"),
    ("S12", "YOUTH SAVINGS"),
]


# Two tenants running the same vendor product, configured differently. This is
# the stand-in for the real environment's "hundreds of institutions, many on
# the same underlying product".
TENANTS = {
    "pinnacle": {
        "name": "PINNACLE COMMUNITY CREDIT UNION",
        "short": "PCCU",
        "accent": "#1f3864",
        "product_version": "7.2",
        # Field order in the lookup form. Harbor reorders these, which is
        # exactly the kind of per-tenant drift that breaks positional
        # automation and does not break accessible-name targeting.
        "lookup_order": ["member", "branch"],
        "member_label": "Member #:",
        "search_button": "Search",
    },
    "harbor": {
        "name": "HARBOR POINT FEDERAL CU",
        "short": "HPFCU",
        "accent": "#6b1f1f",
        "product_version": "7.2",
        "lookup_order": ["branch", "member"],
        # Same control, different caption — the one thing that *does* require
        # a per-tenant override, and the reason TenantOverride exists.
        "member_label": "Account Number:",
        "search_button": "Find",
    },
}

DEFAULT_TENANT = "pinnacle"
