"""Certification campaign reports: progress, decisions, reviewers and audit detail, as an Excel
dashboard (KPI tiles, doughnut and stacked bar charts, filterable tables) and a PDF.

Data comes from the certification campaigns, certifications, certification summaries and
campaign filters APIs. :func:`fetch` reads them; everything else works on plain JSON dicts, so it
can be tested without a tenant.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from isc_reporter import dashboard
from isc_reporter.dashboard import AMBER, BLUE, GREEN, GREY, RED, Chart
from isc_reporter.errors import UsageError

log = logging.getLogger("isc_reporter")

STATUSES = ("STAGED", "ACTIVATING", "ACTIVE", "COMPLETING", "COMPLETED", "ERROR", "ARCHIVED",
            "CANCELED", "PENDING")
ACCESS_TYPES = (("entitlement", "Entitlements"), ("accessProfile", "Access profiles"),
                ("role", "Roles"), ("account", "Accounts"))
DECISIONS = {"APPROVE": "Approved", "REVOKE": "Revoked"}
PENDING = "Pending"
STATUS_COLOURS = {"ACTIVE": BLUE, "COMPLETED": GREEN, "STAGED": "90A4AE", "ERROR": RED,
                  "COMPLETING": "26A69A", "ACTIVATING": "42A5F5", "CANCELED": "757575",
                  "ARCHIVED": "B0BEC5", "PENDING": AMBER}
TOP_REVIEWERS = 15
NOTES = {"ROLE_COMPOSITION": "Role composition reviews are not identity certifications: progress comes "
                             "from the campaign, there is no reviewer or decision detail."}


# -- data -----------------------------------------------------------------------


@dataclass
class CertData:
    campaigns: list[dict]
    certifications: list[dict] = field(default_factory=list)   # + "decisionSummary"
    items: list[dict] = field(default_factory=list)            # + "certification" context
    filters: list[dict] = field(default_factory=list)
    scope: str = ""
    origin: str = ""
    with_items: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def fetch(isc, *, campaigns: list[str] | None = None, statuses: list[str] | None = None,
          since: str | None = None, summaries: bool = True, items: bool = False) -> CertData:
    """Read campaigns (filtered by name/id, status and creation date), their certifications,
    decision summaries and, with *items*, every reviewed access item."""
    from isc_reporter.api import call, list_all, model_json
    from isc_reporter.query import quote

    filters = f"status in ({', '.join(quote(s) for s in statuses)})" if statuses else None
    found = [model_json(getattr(c, "actual_instance", None) or c) for c in
             list_all(isc.campaigns.get_active_campaigns_v1, detail="FULL", filters=filters)]
    if campaigns:
        wanted = {c.lower() for c in campaigns}

        def keys(campaign):
            return {campaign.get("id", "").lower(), campaign.get("name", "").lower()}

        found = [c for c in found if keys(c) & wanted]
        missing = wanted - set().union(*(keys(c) for c in found))
        if missing:
            raise UsageError(f"campaign(s) not found: {', '.join(sorted(missing))}")
    if since:
        found = [c for c in found if str(c.get("created", ""))[:10] >= since]
    log.info("%d campaign(s)", len(found))

    data = CertData(found, with_items=items)
    for campaign in found:
        certs = [model_json(c) for c in list_all(isc.certifications.list_identity_certifications_v1,
                                                 filters=f"campaign.id eq {quote(campaign['id'])}")]
        for cert in certs:
            cert.setdefault("campaign", {"id": campaign["id"], "name": campaign.get("name", "")})
            if summaries:
                cert["decisionSummary"] = model_json(call(
                    isc.certification_summaries.get_identity_decision_summary_v1, id=cert["id"]))
            if items:
                context = {"campaign": campaign.get("name", ""),
                           "reviewer": (cert.get("reviewer") or {}).get("name", "")}
                for item in list_all(isc.certifications.list_identity_access_review_items_v1, id=cert["id"]):
                    data.items.append({**model_json(item), "certification": context})
        data.certifications += certs
        log.info("  %s: %d certification(s)", campaign.get("name", ""), len(certs))

    start, page = 0, 250
    while True:
        response = call(isc.campaign_filters.list_campaign_filters_v1, limit=page, start=start,
                        include_system_filters=True)
        batch = [model_json(f) for f in (getattr(response, "items", None) or [])]
        data.filters += batch
        if len(batch) < page:
            break
        start += page
    return data


# -- analysis ---------------------------------------------------------------------


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def day(value) -> str:
    return str(value or "")[:10]


def ratio(part, whole) -> float | None:
    return part / whole if whole else None


def decision_counts(summary: dict | None) -> dict | None:
    """Approved / revoked / made / total, overall and per access type, from a decision summary."""
    if not summary:
        return None
    counts = {"approved": 0, "revoked": 0, "made": 0, "total": 0, "by_type": {}}
    for key, label in ACCESS_TYPES:
        plural = "accessProfiles" if key == "accessProfile" else f"{key}s"
        row = {"approved": summary.get(f"{plural}Approved") or 0,
               "revoked": summary.get(f"{plural}Revoked") or 0,
               "made": summary.get(f"{key}DecisionsMade") or 0,
               "total": summary.get(f"{key}DecisionsTotal") or 0}
        counts["by_type"][label] = row
        for name in ("approved", "revoked", "made", "total"):
            counts[name] += row[name]
    return counts


def is_overdue(deadline, completed: bool, now: datetime) -> bool:
    moment = parse_time(deadline)
    return bool(moment and not completed and moment < now)


def certification_rows(data: CertData) -> list[dict]:
    rows = []
    for cert in data.certifications:
        counts = decision_counts(cert.get("decisionSummary"))
        reviewer = cert.get("reviewer") or {}
        made, total = cert.get("decisionsMade") or 0, cert.get("decisionsTotal") or 0
        rows.append({
            "Campaign": (cert.get("campaign") or {}).get("name", ""),
            "Reviewer": reviewer.get("name", ""), "Reviewer email": reviewer.get("email", ""),
            "Identities done": cert.get("identitiesCompleted") or 0,
            "Identities": cert.get("identitiesTotal") or 0,
            "Decisions made": made, "Decisions": total, "Progress": ratio(made, total),
            "Approved": counts["approved"] if counts else None,
            "Revoked": counts["revoked"] if counts else None,
            "Pending": total - made,
            "Due": day(cert.get("due")), "Signed off": day(cert.get("signed")),
            "Phase": cert.get("phase", ""),
            "Overdue": "yes" if is_overdue(cert.get("due"), bool(cert.get("completed")), data.now) else "no",
            "Errors": cert.get("errorMessage") or ("yes" if cert.get("hasErrors") else ""),
        })
    return rows


def campaign_rows(data: CertData) -> list[dict]:
    certs = certification_rows(data)
    by_campaign = defaultdict(list)
    for row, cert in zip(certs, data.certifications, strict=True):
        by_campaign[(cert.get("campaign") or {}).get("id")].append(row)
    filters = {f.get("id"): f.get("name", "") for f in data.filters}
    rows = []
    for campaign in data.campaigns:
        mine = by_campaign.get(campaign.get("id"), [])
        total = campaign.get("totalCertifications") or len(mine)
        done = campaign.get("completedCertifications") or 0
        made = sum(r["Decisions made"] for r in mine)
        decisions = sum(r["Decisions"] for r in mine)
        known = [r for r in mine if r["Approved"] is not None]
        approved = sum(r["Approved"] for r in known) if known else None
        revoked = sum(r["Revoked"] for r in known) if known else None
        deadline = parse_time(campaign.get("deadline"))
        finished = campaign.get("status") in ("COMPLETED", "ARCHIVED", "CANCELED")
        days_left = (deadline - data.now).days if deadline and not finished else None
        filter_ref = campaign.get("filter") or {}
        rows.append({
            "Campaign": campaign.get("name", ""), "Type": campaign.get("type", ""),
            "Status": campaign.get("status", ""), "Created": day(campaign.get("created")),
            "Deadline": day(campaign.get("deadline")), "Days left": days_left,
            "Certifications": total, "Completed": done, "Progress": ratio(done, total),
            "Decisions made": made, "Decisions": decisions, "Decision progress": ratio(made, decisions),
            "Approved": approved, "Revoked": revoked,
            "Revocation rate": ratio(revoked, (approved or 0) + (revoked or 0)) if known else None,
            "Overdue": "yes" if days_left is not None and days_left < 0 else "no",
            "Auto revoke": "yes" if campaign.get("autoRevokeAllowed") else "no",
            "Filter": filters.get(filter_ref.get("id"), filter_ref.get("name", "")),
            "Note": NOTES.get(campaign.get("type", ""), ""),
            "Description": campaign.get("description") or "",
        })
    return rows


def reviewer_rows(data: CertData) -> list[dict]:
    totals: dict[tuple, Counter] = defaultdict(Counter)
    for row in certification_rows(data):
        key = (row["Reviewer"], row["Reviewer email"])
        totals[key].update({"Certifications": 1, "Signed off": 1 if row["Signed off"] else 0,
                            "Decisions made": row["Decisions made"], "Decisions": row["Decisions"],
                            "Approved": row["Approved"] or 0, "Revoked": row["Revoked"] or 0,
                            "Overdue certifications": 1 if row["Overdue"] == "yes" else 0})
    rows = [{"Reviewer": name, "Email": email, **{k: c[k] for k in (
        "Certifications", "Signed off", "Decisions made", "Decisions", "Approved", "Revoked",
        "Overdue certifications")}, "Pending": c["Decisions"] - c["Decisions made"],
        "Progress": ratio(c["Decisions made"], c["Decisions"])}
        for (name, email), c in totals.items()]
    return sorted(rows, key=lambda r: (-r["Pending"], r["Reviewer"].lower()))


def item_rows(data: CertData) -> list[dict]:
    rows = []
    for item in data.items:
        summary = item.get("accessSummary") or {}
        access = summary.get("access") or {}
        detail = summary.get("entitlement") or summary.get("accessProfile") or summary.get("role") or {}
        identity = item.get("identitySummary") or {}
        context = item.get("certification") or {}
        rows.append({
            "Campaign": context.get("campaign", ""), "Reviewer": context.get("reviewer", ""),
            "Identity": identity.get("name", ""),
            "Access type": (access.get("type") or "").replace("_", " ").title(),
            "Access": access.get("name") or detail.get("name", ""),
            "Source": detail.get("sourceName") or (detail.get("source") or {}).get("name", ""),
            "Privileged": "yes" if detail.get("privileged") else "no",
            "Decision": DECISIONS.get(item.get("decision") or "", PENDING),
            "New access": "yes" if item.get("newAccess") else "no",
            "Comments": item.get("comments") or "",
        })
    return rows


def identity_rows(data: CertData) -> list[dict]:
    """One line per identity and campaign, with its access grouped by decision."""
    grouped: dict[tuple, dict] = {}
    for row in item_rows(data):
        key = (row["Campaign"], row["Identity"])
        entry = grouped.setdefault(key, {"Campaign": row["Campaign"], "Identity": row["Identity"],
                                         "Reviewers": set(), "Approved": [], "Revoked": [], PENDING: []})
        entry["Reviewers"].add(row["Reviewer"])
        entry[row["Decision"]].append(row["Access"])
    return [{"Campaign": e["Campaign"], "Identity": e["Identity"],
             "Reviewers": " | ".join(sorted(e["Reviewers"])),
             "Approved": len(e["Approved"]), "Revoked": len(e["Revoked"]), "Pending": len(e[PENDING]),
             "Revoked access": " | ".join(e["Revoked"]), "Pending access": " | ".join(e[PENDING])}
            for e in sorted(grouped.values(), key=lambda e: (e["Campaign"], e["Identity"].lower()))]


def filter_rows(data: CertData) -> list[dict]:
    used = defaultdict(list)
    for campaign in data.campaigns:
        filter_id = (campaign.get("filter") or {}).get("id")
        if filter_id:
            used[filter_id].append(campaign.get("name", ""))
    rows = []
    for f in data.filters:
        criteria = [" ".join(str(c.get(k)) for k in ("type", "property", "operation", "value")
                             if c.get(k) not in (None, "")) for c in f.get("criteriaList") or []]
        rows.append({"Filter": f.get("name", ""), "Mode": f.get("mode", ""),
                     "Criteria": " | ".join(criteria), "Owner": f.get("owner") or "",
                     "System filter": "yes" if f.get("isSystemFilter") else "no",
                     "Used by campaigns in this report": " | ".join(used.get(f.get("id"), [])),
                     "Description": f.get("description") or ""})
    return rows


def kpis(data: CertData) -> list[tuple[str, str, str]]:
    """``(label, value, colour)`` tiles for the dashboard."""
    campaigns = campaign_rows(data)
    certs = certification_rows(data)
    total = sum(r["Certifications"] for r in campaigns)
    done = sum(r["Completed"] for r in campaigns)
    made = sum(r["Decisions made"] for r in certs)
    decisions = sum(r["Decisions"] for r in certs)
    approved = sum(r["Approved"] or 0 for r in certs)
    revoked = sum(r["Revoked"] or 0 for r in certs)
    active = sum(1 for r in campaigns if r["Status"] == "ACTIVE")
    overdue = sum(1 for r in certs if r["Overdue"] == "yes")

    def pct(part, whole):
        return f"{part / whole:.0%}" if whole else "-"

    return [
        ("Campaigns", f"{len(campaigns)}  ({active} active)", BLUE),
        ("Certifications completed", f"{done} / {total}  ({pct(done, total)})", BLUE),
        ("Decisions made", f"{made} / {decisions}  ({pct(made, decisions)})", BLUE),
        ("Reviewers", str(len({r['Reviewer'] for r in certs})), BLUE),
        ("Approved", str(approved), GREEN),
        ("Revoked", f"{revoked}  ({pct(revoked, approved + revoked)} of decisions)", RED),
        ("Pending decisions", str(decisions - made), GREY if decisions == made else AMBER),
        ("Overdue certifications", str(overdue), RED if overdue else GREEN),
    ]


def charts(data: CertData) -> list[Chart]:
    campaigns = campaign_rows(data)
    certs = certification_rows(data)
    result = []
    status = Counter(r["Status"] for r in campaigns)
    if status:
        labels = sorted(status, key=lambda s: STATUSES.index(s) if s in STATUSES else 99)
        result.append(Chart("Campaigns by status", "doughnut", labels,
                            [("Campaigns", [status[s] for s in labels], "")]))
    approved = sum(r["Approved"] or 0 for r in certs)
    revoked = sum(r["Revoked"] or 0 for r in certs)
    pending = sum(r["Pending"] for r in certs)
    if approved or revoked or pending:
        result.append(Chart("Decisions", "doughnut", ["Approved", "Revoked", "Pending"],
                            [("Decisions", [approved, revoked, pending], "")]))
    if campaigns:
        shown = sorted(campaigns, key=lambda r: r["Created"], reverse=True)[:15]
        result.append(Chart("Campaign progress (certifications completed)", "bar",
                            [_short(r["Campaign"]) for r in shown],
                            [("Completed", [r["Progress"] or 0 for r in shown], BLUE)], percent=True))
    by_type = defaultdict(Counter)
    for cert in data.certifications:
        counts = decision_counts(cert.get("decisionSummary"))
        for label, row in (counts or {}).get("by_type", {}).items():
            by_type[label].update({"approved": row["approved"], "revoked": row["revoked"],
                                   "pending": row["total"] - row["made"]})
    types = [label for _, label in ACCESS_TYPES if sum(by_type[label].values())]
    if types:
        result.append(Chart("Decisions by access type", "stacked", types, [
            ("Approved", [by_type[t]["approved"] for t in types], GREEN),
            ("Revoked", [by_type[t]["revoked"] for t in types], RED),
            ("Pending", [by_type[t]["pending"] for t in types], GREY)]))
    reviewers = sorted(reviewer_rows(data), key=lambda r: -r["Decisions"])[:TOP_REVIEWERS]
    if reviewers:
        result.append(Chart(f"Reviewer progress (top {len(reviewers)} by workload)", "stacked",
                            [_short(r["Reviewer"]) for r in reviewers], [
                                ("Decided", [r["Decisions made"] for r in reviewers], GREEN),
                                ("Pending", [r["Pending"] for r in reviewers], GREY)]))
    if data.with_items and data.items:
        revoked_types = Counter(r["Access type"] for r in item_rows(data) if r["Decision"] == "Revoked")
        if revoked_types:
            labels = [label for label, _ in revoked_types.most_common()]
            result.append(Chart("Revocations by access type", "doughnut", labels,
                                [("Revoked", [revoked_types[x] for x in labels], "")]))
    return result


def _short(text: str, size: int = 40) -> str:
    return text if len(text) <= size else text[:size - 1] + "…"


# -- rendering ----------------------------------------------------------------------

PERCENT_COLUMNS = {"Progress", "Decision progress", "Revocation rate"}
WIDE_COLUMNS = {"Description", "Note", "Criteria", "Revoked access", "Pending access", "Comments",
                "Used by campaigns in this report", "Access", "Errors"}
READ_ME = [
    ("Dashboard", "Key figures and charts for the campaigns in this report."),
    ("Campaigns", "One line per campaign: certifications completed, decisions, revocation rate, "
                  "deadline and days left (negative = overdue)."),
    ("Certifications", "One line per reviewer certification of each campaign."),
    ("Reviewers", "Workload and progress per reviewer across the campaigns, most pending first."),
    ("Identities", "With --items: one line per identity and campaign, access grouped by decision."),
    ("Decisions", "With --items: one line per reviewed access item (audit evidence)."),
    ("Filters", "Campaign filters of the tenant and the campaigns of this report that use them."),
    ("Chart data", "The numbers behind each chart."),
    ("Approved / Revoked", "From the certification decision summaries (entitlements, access "
                           "profiles, roles and accounts)."),
    ("Overdue", "Not completed and past its deadline / due date."),
]


def build_dashboard(data: CertData, title: str) -> dashboard.Dashboard:
    def table(name, rows):
        return dashboard.Table(name, rows, percent=PERCENT_COLUMNS, wide=WIDE_COLUMNS,
                               bars={"Progress", "Decision progress"}, red_yes={"Overdue"},
                               red_positive={"Overdue certifications", "Revoked"},
                               text_colours={"Decision": {"Approved": "green", "Revoked": "red",
                                                          PENDING: "grey"}})

    tables = [table("Campaigns", campaign_rows(data)), table("Certifications", certification_rows(data)),
              table("Reviewers", reviewer_rows(data))]
    if data.with_items:
        tables += [table("Identities", identity_rows(data)), table("Decisions", item_rows(data))]
    tables.append(table("Filters", filter_rows(data)))
    sections = [
        dashboard.Section("Campaigns", campaign_rows(data), [
            "Campaign", "Status", "Deadline", "Days left", "Completed", "Certifications", "Progress",
            "Decisions made", "Decisions", "Approved", "Revoked", "Revocation rate", "Overdue"]),
        dashboard.Section("Reviewers", reviewer_rows(data), [
            "Reviewer", "Certifications", "Signed off", "Decisions made", "Decisions", "Pending",
            "Progress", "Approved", "Revoked", "Overdue certifications"]),
    ]
    if data.with_items:
        revoked = [r for r in item_rows(data) if r["Decision"] == "Revoked"]
        sections.append(dashboard.Section("Revoked access", revoked, [
            "Campaign", "Reviewer", "Identity", "Access type", "Access", "Source", "Privileged",
            "Comments"]))
    return dashboard.Dashboard(
        title, dashboard.subtitle(data.origin, data.scope), kpis(data), charts(data), tables,
        sections, READ_ME,
        colours={"Approved": GREEN, "Revoked": RED, "Pending": GREY, **STATUS_COLOURS},
        wide_pdf={"Campaign", "Reviewer", "Access", "Identity", "Comments"},
        alert=lambda row: row.get("Overdue") == "yes" or bool(row.get("Overdue certifications")))


def write_xlsx(data: CertData, path: str, title: str) -> str:
    return dashboard.write_xlsx(build_dashboard(data, title), path)


def write_pdf(data: CertData, path: str, title: str, max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(data, title), path, max_rows)
