"""Sample reports from fictional data, to see what isc-reporter produces without a tenant.

Every name, id and value here is invented; the generator is seeded, so the output is the same
on every run.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

FIRST = [
    "Alice",
    "Bruno",
    "Chloe",
    "David",
    "Emma",
    "Farid",
    "Grace",
    "Hugo",
    "Ines",
    "Jonas",
    "Karima",
    "Leo",
    "Maya",
    "Nora",
    "Omar",
    "Paula",
    "Quentin",
    "Rita",
    "Samir",
    "Tess",
]
LAST = ["Martin", "Bernard", "Dubois", "Garcia", "Muller", "Rossi", "Silva", "Nowak", "Haddad", "Novak"]
DEPARTMENTS = ["Finance", "IT", "HR", "Sales", "Legal", "Operations", "Marketing"]
ROLES = [
    "Employee Base",
    "Finance Analyst",
    "IT Administrator",
    "HR Partner",
    "Sales Representative",
    "Auditor",
]
PROFILES = [
    "Finance - Read",
    "Finance - Approver",
    "VPN",
    "Mail",
    "CRM - User",
    "HR Portal",
    "Ticketing",
    "Wiki",
]
SOURCES = ["Active Directory", "SAP", "Salesforce"]


def _id(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(32))


def identities(rng: random.Random, count: int = 240) -> list[dict]:
    docs = []
    for i in range(count):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        access = [{"type": "ROLE", "id": r, "name": r} for r in rng.sample(ROLES, rng.randint(0, 2))]
        access += [
            {"type": "ACCESS_PROFILE", "id": p, "name": p} for p in rng.sample(PROFILES, rng.randint(0, 4))
        ]
        access += [
            {
                "type": "ENTITLEMENT",
                "id": f"e{n}",
                "name": f"GRP-{n:03d}",
                "displayName": f"GRP-{n:03d}",
                "source": {"name": rng.choice(SOURCES[:2])},
            }
            for n in rng.sample(range(60), rng.randint(0, 7))
        ]
        accounts = [
            {"name": f"{first[0]}{last}{i}".lower(), "source": {"name": s}}
            for s in rng.sample(SOURCES, rng.randint(1, 3))
        ]
        docs.append(
            {
                "id": _id(rng),
                "name": f"{first}.{last}{i}".lower(),
                "displayName": f"{first} {last}",
                "email": f"{first}.{last}{i}@example.com".lower(),
                "firstName": first,
                "lastName": last,
                "attributes": {
                    "uid": f"u{i:04d}",
                    "employeeNumber": f"E{i:05d}",
                    "department": rng.choice(DEPARTMENTS),
                    "cloudLifecycleState": rng.choice(["active"] * 9 + ["inactive", "prehire"]),
                },
                "identityProfile": {"name": rng.choice(["Employees", "Contractors"])},
                "manager": {"id": "m", "displayName": f"{rng.choice(FIRST)} {rng.choice(LAST)}"}
                if i % 9
                else None,
                "inactive": i % 17 == 0,
                "accessCount": len(access),
                "accountCount": len(accounts),
                "access": access,
                "accounts": accounts,
                "created": "2025-01-15T08:00:00Z",
            }
        )
    return docs


def campaigns(rng: random.Random, now: datetime):
    from isc_reporter.certification_reports import CertData

    reviewers = [f"{rng.choice(FIRST)} {rng.choice(LAST)}" for _ in range(8)]
    campaign_list, certs = [], []
    for number, (name, status, offset) in enumerate(
        [
            ("Q1 Manager Certification", "COMPLETED", -200),
            ("Q2 Manager Certification", "COMPLETED", -100),
            ("Q3 Manager Certification", "ACTIVE", -10),
            ("Privileged Access Review", "ACTIVE", 20),
            ("SAP Source Owner Review", "ACTIVE", 35),
            ("Q4 Manager Certification", "STAGED", 90),
        ]
    ):
        cid = f"c{number}"
        total = 0 if status == "STAGED" else rng.randint(4, 8)
        done = total if status == "COMPLETED" else rng.randint(0, total)
        campaign_list.append(
            {
                "id": cid,
                "name": name,
                "type": "MANAGER" if "Manager" in name else "SEARCH",
                "status": status,
                "created": f"{(now + timedelta(days=offset - 30)):%Y-%m-%d}T08:00:00Z",
                "deadline": f"{(now + timedelta(days=offset)):%Y-%m-%d}T23:59:59Z",
                "totalCertifications": total,
                "completedCertifications": done,
                "autoRevokeAllowed": True,
            }
        )
        for c in range(total):
            decisions = rng.randint(8, 40)
            made = decisions if c < done else rng.randint(0, decisions)
            revoked = rng.randint(0, max(1, made // 6))
            role_total = rng.randint(1, 4)
            role_made = min(role_total, made)
            certs.append(
                {
                    "id": f"{cid}-{c}",
                    "campaign": {"id": cid, "name": name},
                    "reviewer": {
                        "name": reviewers[(number + c) % len(reviewers)],
                        "email": "reviewer@example.com",
                    },
                    "identitiesCompleted": 3 if c < done else 1,
                    "identitiesTotal": 3,
                    "decisionsMade": made,
                    "decisionsTotal": decisions,
                    "due": f"{(now + timedelta(days=offset)):%Y-%m-%d}T23:59:59Z",
                    "signed": f"{now:%Y-%m-%d}T10:00:00Z" if c < done else None,
                    "completed": c < done,
                    "phase": "SIGNED" if c < done else "ACTIVE",
                    "decisionSummary": {
                        "entitlementsApproved": made - role_made - revoked,
                        "entitlementsRevoked": revoked,
                        "rolesApproved": role_made,
                        "entitlementDecisionsMade": made - role_made,
                        "entitlementDecisionsTotal": decisions - role_total,
                        "roleDecisionsMade": role_made,
                        "roleDecisionsTotal": role_total,
                    },
                }
            )
    filters = [
        {
            "id": "f1",
            "name": "Privileged entitlements only",
            "mode": "INCLUSION",
            "criteriaList": [{"type": "ENTITLEMENT", "operation": "EQUALS", "value": "privileged"}],
        }
    ]
    return CertData(campaign_list, certs, filters=filters, now=now, origin="demo", scope="all statuses")


def history(rng: random.Random, now: datetime, people: list[dict]):
    from isc_reporter.attribute_history import HistoryData

    start = now - timedelta(days=30)
    data = HistoryData(start, now, ["department", "manager", "location"], identities=people[:60])
    for person in people[:60]:
        for _ in range(rng.choice([0, 0, 1, 1, 2])):
            attribute = rng.choice(["department", "manager", "location"])
            values = {
                "department": DEPARTMENTS,
                "manager": [f"{first} {last}" for first in FIRST[:6] for last in LAST[:2]],
                "location": ["Paris", "Lyon", "Brussels", "Geneva", "Casablanca"],
            }[attribute]
            old, new = rng.sample(values, 2)
            moment = start + timedelta(days=rng.randint(0, 29), hours=rng.randint(7, 19))
            data.changes.append(
                {"identity": person, "time": moment, "attribute": attribute, "old": old, "new": new}
            )
    data.changes.sort(key=lambda c: c["time"])
    data.origin = "demo"
    return data


def sample_policies() -> list[dict]:
    """Provisioning policies of two fictional sources (they read identity attributes)."""
    def read(name):
        return {"type": "identityAttribute", "attributes": {"name": name}}

    return [
        {"source": "Active Directory", "name": "Account", "usageType": "CREATE", "fields": [
            {"name": "sAMAccountName", "transform": {"type": "rule", "attributes": {
                "name": "Create Unique LDAP Attribute", "template": "{firstname}.{lastname}"}}},
            {"name": "mail", "transform": read("email")},
            {"name": "department", "transform": read("department")},
            {"name": "displayName", "transform": read("displayName")},
            {"name": "password", "type": "secret", "isRequired": True}]},
        {"source": "Active Directory", "name": "Update", "usageType": "UPDATE", "fields": [
            {"name": "manager", "transform": {"type": "getReferenceIdentityAttribute", "attributes": {
                "uid": "manager", "attributeName": "uid"}}},
            {"name": "department", "transform": read("department")}]},
    ]


def sample_profiles() -> tuple[list[dict], dict[str, dict]]:
    def account(attribute, source="HR System"):
        return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}

    def mapping(name, definition):
        return {"identityAttributeName": name, "transformDefinition": definition}

    def profile(name, source, priority, mappings, count):
        return {
            "id": name,
            "name": name,
            "priority": priority,
            "identityCount": count,
            "authoritativeSource": {"type": "SOURCE", "id": source, "name": source},
            "owner": {"name": "IAM Team"},
            "identityAttributeConfig": {"attributeTransforms": mappings},
        }

    employees = profile(
        "Employees",
        "HR System",
        10,
        [
            mapping("uid", account("employeeId")),
            mapping("email", account("mail", "Active Directory")),
            mapping("firstname", account("givenName")),
            mapping("lastname", account("surname")),
            mapping(
                "displayName",
                {
                    "type": "concat",
                    "attributes": {
                        "values": [
                            account("givenName"),
                            {"type": "static", "attributes": {"value": " "}},
                            account("surname"),
                        ]
                    },
                },
            ),
            mapping("department", account("department")),
            mapping("manager", account("managerId")),
            mapping(
                "startDate",
                {"type": "reference", "attributes": {"id": "Date to ISO8601", "input": account("hireDate")}},
            ),
            mapping("cloudLifecycleState", {"type": "reference", "attributes": {"id": "Lifecycle state"}}),
        ],
        1840,
    )
    contractors = profile(
        "Contractors",
        "Contractor Register",
        20,
        [
            mapping("uid", account("login", "Contractor Register")),
            mapping("email", account("email", "Contractor Register")),
            mapping("lastname", account("lastName", "Contractor Register")),
            mapping("cloudLifecycleState", account("status", "Contractor Register")),
        ],
        312,
    )
    transforms = {
        "Date to ISO8601": {
            "name": "Date to ISO8601",
            "type": "dateFormat",
            "attributes": {"inputFormat": "dd/MM/yyyy", "outputFormat": "ISO8601"},
        },
        "Lifecycle state": {
            "name": "Lifecycle state",
            "type": "dateCompare",
            "attributes": {
                "firstDate": account("hireDate"),
                "secondDate": "now",
                "operator": "LTE",
                "positiveCondition": "active",
                "negativeCondition": "prehire",
            },
        },
    }
    return [employees, contractors], transforms


def run(folder: str) -> list[str]:
    """Write every sample report into *folder*; returns the files written."""
    from isc_reporter import (
        attribute_history,
        certification_reports,
        identity_profiles,
        report_writers,
        reporting,
        source_migration,
    )
    from isc_reporter.source_migration import Source

    rng = random.Random(7)
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    os.makedirs(folder, exist_ok=True)
    out = []
    people = identities(rng)
    search = reporting.build(
        reporting.report_type("identities"), people, query="*", expand="access", origin="demo"
    )
    out += [
        report_writers.write_xlsx(search, os.path.join(folder, "search-identities.xlsx")),
        report_writers.write_pdf(search, os.path.join(folder, "search-identities.pdf")),
    ]
    certs = campaigns(rng, now)
    out += [
        certification_reports.write_xlsx(
            certs, os.path.join(folder, "campaigns.xlsx"), "Certification campaigns"
        ),
        certification_reports.write_pdf(
            certs, os.path.join(folder, "campaigns.pdf"), "Certification campaigns"
        ),
    ]
    changes = history(rng, now, people)
    out += [
        attribute_history.write_xlsx(
            changes, os.path.join(folder, "identity-changes.xlsx"), "Identity attribute changes"
        ),
        attribute_history.write_pdf(
            changes, os.path.join(folder, "identity-changes.pdf"), "Identity attribute changes"
        ),
    ]
    profiles, transforms = sample_profiles()
    for extension, writer in (("xlsx", identity_profiles.write_xlsx), ("pdf", identity_profiles.write_pdf)):
        path = os.path.join(folder, f"identity-profiles.{extension}")
        writer(path, profiles, transforms, origin="demo", policies=sample_policies())
        out.append(path)
    plan = source_migration.analyse(
        profiles,
        transforms,
        Source("HR System"),
        Source("New HR", "", ["employeeId", "givenName", "surname", "hireDate", "department"]),
        {"managerId": "manager_id"},
    )
    out.append(source_migration.write_outputs(plan, os.path.join(folder, "source-migration")))
    return out
