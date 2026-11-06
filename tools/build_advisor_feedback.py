"""Збирає workflows/support-agent-advisor-feedback.json (флоу «Автоматизатор: Advisor Feedback»).

Раз на добу шукає в заявках, де радник залишив приватну підказку (рядки аудиту kind=advisor, бойові), пізніші коментарі інженерів
«корисно» або «не те» і записує їх у таблицю support_agent_advisor_feedback (journal_id унікальний: повторно не пишемо).
Нічого не змінює в Redmine, лише читає.

Запуск: python tools/build_advisor_feedback.py [--redmine-cred ID --redmine-name N --audit-table ID --feedback-table ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TICKETS_JS = r"""
const cfg = $('Config').first().json;
const since = Date.now() - Number(cfg.days_back) * 86400000;
const ids = [];
for (const { json: r } of $('[Data Table] Get Audit').all()) {
  if (r.kind === 'advisor' && r.dry_run === false && r.ticket_id && Date.parse(r.at) >= since && !ids.includes(r.ticket_id)) ids.push(r.ticket_id);
}
return ids.slice(0, Number(cfg.max_tickets)).map(id => ({ json: { ticket_id: id } }));
"""

RATINGS_JS = r"""
const cfg = $('Config').first().json;
const have = new Set($('[Data Table] Get Feedback').all().map(i => String(i.json.journal_id)));
const rows = [];
for (const { json: h } of $input.all()) {
  const issue = h.issue || h;
  if (!issue.id) continue;
  const js = (issue.journals || []).slice().sort((a, b) => String(a.created_on).localeCompare(String(b.created_on)));
  const adv = js.find(j => String(j.notes || '').startsWith(cfg.advisor_prefix));
  if (!adv) continue;
  for (const j of js) {
    if (String(j.created_on) <= String(adv.created_on) || have.has(String(j.id))) continue;
    if (j.user && adv.user && String(j.user.id) === String(adv.user.id)) continue;  // не оцінки самого бота
    const m = String(j.notes || '').trim().toLowerCase().match(/^(корисно|не те)(?=$|[\s.,!:;)\-–])/);
    if (!m) continue;
    rows.push({ json: { journal_id: j.id, ticket_id: issue.id, rating: m[1] === 'корисно' ? 'up' : 'down', comment: String(j.notes).trim().slice(0, 300),
                        user_id: j.user ? j.user.id : 0, at: j.created_on, collected_at: new Date().toISOString() } });
  }
}
return rows;
"""

COLS = [("journal_id", "number"), ("ticket_id", "number"), ("rating", "string"), ("comment", "string"), ("user_id", "number"), ("at", "string"), ("collected_at", "string")]


def build(rm_id, rm_name, audit_table, fb_table, trigger=True):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    nodes, conn = [], {}

    def node(name, type_, params, x, y, ver=1, **extra):
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        n.update(extra)
        nodes.append(n)

    tbl = lambda t: {"__rl": True, "mode": "id", "value": t}
    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Advisor Feedback\n"
        "Раз на добу збирає оцінки підказок радника: коментарі інженерів «корисно» або «не те» в заявках, де радник залишив приватну підказку.\n"
        "Пише в таблицю support_agent_advisor_feedback (журнал коментаря унікальний, дублів нема). Redmine лише читає."), "height": 150, "width": 620}, -200, -260)
    if trigger:
        node("Every Day 08:00", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 8}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "advisor_prefix", "value": "Радник (етап 1", "type": "string"},
        {"id": "a2", "name": "days_back", "value": 30, "type": "number"},
        {"id": "a3", "name": "max_tickets", "value": 60, "type": "number"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Audit", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": tbl(audit_table),
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Data Table] Get Feedback", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": tbl(fb_table),
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 660, 0, ver=1.1, alwaysOutputData=True, executeOnce=True)
    node("Code: Tickets To Check", "n8n-nodes-base.code", {"jsCode": TICKETS_JS.strip()}, 880, 0, ver=2)
    node("[Redmine] Get Ticket History", "n8n-nodes-redmine.redmine", {"operation": "get", "issueId": "={{ $json.ticket_id }}", "include": ["journals"], "options": {}},
         1100, 0, credentials=rmc, onError="continueRegularOutput")
    node("Code: Find Ratings", "n8n-nodes-base.code", {"jsCode": RATINGS_JS.strip()}, 1320, 0, ver=2)
    node("[Data Table] Save Feedback", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": tbl(fb_table), "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $json.%s }}" % c for c, _ in COLS},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in COLS]},
        "options": {}}, 1540, 0, ver=1.1)
    chain = (["Every Day 08:00"] if trigger else []) + ["Config", "[Data Table] Get Audit", "[Data Table] Get Feedback", "Code: Tickets To Check",
             "[Redmine] Get Ticket History", "Code: Find Ratings", "[Data Table] Save Feedback"]
    for a, b in zip(chain, chain[1:]):
        conn[a] = {"main": [[{"node": b, "type": "main", "index": 0}]]}
    settings = {"executionOrder": "v1", "timezone": "Europe/Kyiv"}
    return {"name": "Сервіс: Advisor Feedback", "nodes": nodes, "connections": conn, "settings": settings}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("audit-table", "<AUDIT_TABLE>"), ("feedback-table", "<FEEDBACK_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-advisor-feedback.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.audit_table, a.feedback_table)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
