"""Збирає workflows/support-agent-redmine-unlock.json (флоу «Support Agent: Redmine Unlock»).

Запуск: python tools/build_unlock.py [--cred-id ID --cred-name NAME --table-id ID]
За замовчуванням підставляються заглушки <REDMINE_CRED>, <AUDIT_TABLE>.
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PARSE_JS = r"""
const cfg = $('Config').first().json;
const allowed = String(cfg.allowed_author_ids).split(',').map(s => s.trim()).filter(Boolean);
const maxCand = Number(cfg.max_candidates);
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
// у бойовому режимі заявки, яких торкався лише DRY RUN, обробляються заново
const seen = new Set($('[Data Table] Get Processed').all()
  .filter(i => i.json.kind !== 'dispatch' && (dry || i.json.dry_run === false)).map(i => String(i.json.ticket_id)));
const DOMAIN = /\.(com|net|org|tech|us|eu|io|ua|ai|dev|ru|pl)$/;
const out = [];
for (const t of ($input.first().json.issues || [])) {
  if (t.assigned_to || seen.has(String(t.id))) continue;
  const text = `${t.subject}\n${t.description || ''}`.toLowerCase();
  // t.ai: підказка диспетчера (тип і логіни від ІІ), коли правила заявку не впізнали
  const isUnlock = (/(розблок|разблок|unlock)/.test(text) && /(redmine|редмайн)/.test(text)) || !!(t.ai && t.ai.type === 'redmine_unlock');
  if (!isUnlock) continue;
  const base = { ticket_id: t.id, author_id: String(t.author && t.author.id) };
  if (!allowed.includes(base.author_id)) {
    out.push({ ...base, kind: 'escalate', reason: 'автор задачі не входить до списку координаторів' });
    continue;
  }
  const mails = (text.match(/[\w.+-]+@(?:example|example)\.com/g) || []).map(m => m.split('@')[0]);
  const clean = text.replace(/https?:\/\/\S+/g, ' ').replace(/[\w.+-]+@[\w.-]+/g, ' ');
  const words = (clean.match(/[a-z0-9][a-z0-9._-]*[a-z0-9]/g) || []).concat(mails);
  let logins = [...new Set(words.filter(w => /[._]/.test(w) && /[a-z].*[a-z]/.test(w) && !DOMAIN.test(w)))];
  if (!logins.length && t.ai) logins = [...new Set((t.ai.logins || []).map(x => String(x).toLowerCase()).filter(x => /^[a-z0-9][a-z0-9._-]*[a-z0-9]$/.test(x) && /[._]/.test(x)))];
  if (logins.length === 0) out.push({ ...base, kind: 'escalate', reason: 'не знайшов у задачі жодного логіну' });
  else if (logins.length > maxCand) out.push({ ...base, kind: 'escalate', reason: `забагато логінів (${logins.length}), максимум ${maxCand}` });
  else out.push({ ...base, kind: 'unlock', logins });
}
return out.map(json => ({ json }));
"""

DECIDE_JS = r"""
const ins = $('Split Out Logins').all();
const found = $input.all();
if (ins.length !== found.length) throw new Error(`Пошук повернув ${found.length} відповідей на ${ins.length} логінів`);
const admins = new Set((($('[Redmine] Get Locked IT Admins').first().json.users) || []).map(u => String(u.id)));
return ins.map((it, i) => {
  const login = it.json.logins;
  const users = ((found[i].json.users) || []).filter(u => String(u.login).toLowerCase() === login);
  const r = { ticket_id: it.json.ticket_id, login, user_id: null, action: 'skip', blocked: false,
              reason: 'не знайдено серед заблокованих (такого логіну немає або обліковка вже активна)' };
  if (users.length > 1) r.reason = 'кілька збігів за логіном';
  else if (users.length === 1) {
    const u = users[0];
    r.user_id = u.id;
    if (u.admin) { r.blocked = true; r.reason = 'адміністратор Redmine, розблоковує тільки людина'; }
    else if (admins.has(String(u.id))) { r.blocked = true; r.reason = 'входить до групи IT, розблоковує тільки людина'; }
    else { r.action = 'unlock'; r.reason = ''; }
  }
  return { json: r };
});
"""

REPORT_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const g = {};
for (const { json: j } of $input.all()) {
  const t = g[j.ticket_id] = g[j.ticket_id] || { id: j.ticket_id, unlock: [], failed: [], esc: null };
  if (j.kind === 'escalate') t.esc = j.reason;
  else if (j.action === 'unlock') {
    if (dry || j.unlocked === 'yes') t.unlock.push(j.login);
    else t.failed.push(`${j.login}: помилка розблокування (${j.error})`);
  } else t.failed.push(`${j.login}: ${j.reason}`);
}
const out = Object.values(g).map(t => {
  const logins = t.unlock.join(', ');
  const failed = t.failed.join('; ');
  let note, kind, close = false, assign = false;
  if (t.esc) {
    kind = 'escalate'; assign = true;
    note = `Агент: автоматично не обробляю. Причина: ${t.esc}. Передано адміністраторам.`;
  } else if (t.unlock.length && !t.failed.length) {
    kind = 'unlock'; close = true;
    note = `Добрий день! Обліковий запис у Redmine розблоковано: ${logins}.`;
  } else {
    kind = 'escalate'; assign = true;
    note = `Агент: розблоковано: ${logins || 'ніхто'}. Не вдалося: ${failed}. Потрібна перевірка людиною, задачу передано адміністраторам.`;
  }
  const details = t.esc || failed;
  if (dry) {
    note = `DRY RUN (змін не внесено). Агент ${kind === 'unlock' ? 'розблокував би ' + logins + ' і закрив би задачу' : 'передав би задачу адміністраторам'}. ${t.esc ? 'Причина: ' + t.esc : (failed ? 'Не вдалося б: ' + failed : '')}`.trim();
    close = false; assign = false;
  }
  return { json: { ticket_id: t.id, kind, note, private: dry || kind === 'escalate', close, assign,
                   logins, outcome: kind, details, dry_run: dry, at: new Date().toISOString() } };
});
return out;
"""


def build(cred_id, cred_name, table_id):
    cred = {"redmineApi": {"id": cred_id, "name": cred_name}}
    nodes, conn = [], {}

    def node(name, type_, params, x, y, ver=1, credentials=None, **extra):
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        if credentials:
            n["credentials"] = credentials
        n.update(extra)
        nodes.append(n)

    def link(a, b, out=0, inp=0):
        conn.setdefault(a, {"main": []})
        m = conn[a]["main"]
        while len(m) <= out:
            m.append([])
        m[out].append({"node": b, "type": "main", "index": inp})

    RM = "n8n-nodes-redmine.redmine"
    cfgv = lambda k: "={{ $('Config').first().json.%s }}" % k
    table = {"__rl": True, "mode": "id", "value": table_id}

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Support Agent: Redmine Unlock\n"
        "Кожні 5 хв бере нові нерозподілені задачі HelpDesk про розблокування Redmine, дістає логіни, "
        "розблоковує заблоковані облікові записи (status 3 -> 1), коментує й закриває задачу. "
        "Усе сумнівне передає групі «IT Адміни».\n\n"
        "**DRY_RUN** (вузол Config): true = нічого не змінює, лише приватна нотатка і запис в аудит."), "height": 190, "width": 560},
        -200, -260, ver=1)

    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "tracker_id", "value": "<TRACKER_ID>", "type": "string"},
        {"id": "a4", "name": "allowed_author_ids", "value": "<COORDINATOR_IDS>", "type": "string"},
        {"id": "a5", "name": "max_candidates", "value": 10, "type": "number"},
        {"id": "a6", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a7", "name": "admins_group_id", "value": "35", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Redmine] Get Locked IT Admins", RM, {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"group_id": cfgv("admins_group_id"), "status": "3"}, "options": {}}, 440, 0, credentials=cred, alwaysOutputData=True)
    node("[Data Table] Get Processed", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 660, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get New Tickets", RM, {"operation": "getAll", "returnAll": True, "filters": {
         "project_id": cfgv("project_id"), "tracker_id": cfgv("tracker_id"), "status_id": "1"}, "options": {}}, 880, 0, credentials=cred)
    node("Code: Parse Tickets", "n8n-nodes-base.code", {"jsCode": PARSE_JS.strip()}, 1100, 0, ver=2)
    node("Route by Type", "n8n-nodes-base.switch", {"rules": {"values": [
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                        "conditions": [{"id": "r1", "leftValue": "={{ $json.kind }}", "rightValue": "unlock",
                                        "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"},
         "renameOutput": True, "outputKey": "unlock"},
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                        "conditions": [{"id": "r2", "leftValue": "={{ $json.kind }}", "rightValue": "escalate",
                                        "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"},
         "renameOutput": True, "outputKey": "escalate"}]}, "options": {}}, 1320, 0, ver=3.2)
    node("Split Out Logins", "n8n-nodes-base.splitOut", {"fieldToSplitOut": "logins", "include": "allOtherFields", "options": {}}, 1540, -120, ver=1)
    node("[Redmine] Find Locked User", RM, {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"name": "={{ $json.logins }}", "status": "3"}, "options": {}}, 1760, -120, credentials=cred, alwaysOutputData=True)
    node("Code: Decide Unlock", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1980, -120, ver=2)
    node("Can Unlock Live?", "n8n-nodes-base.if", {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
         "conditions": [{"id": "c1", "leftValue": "={{ $json.action === 'unlock' && $('Config').first().json.dry_run === false }}",
                         "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"},
         "options": {}}, 2200, -120, ver=2.2)
    node("[Redmine] Unlock User", RM, {"resource": "user", "operation": "update", "userId": "={{ $json.user_id }}",
         "additionalFields": {"status": "1"}, "options": {}}, 2420, -220, credentials=cred, onError="continueErrorOutput")
    node("Mark Unlocked", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "m1", "name": "ticket_id", "value": "={{ $('Code: Decide Unlock').item.json.ticket_id }}", "type": "number"},
        {"id": "m2", "name": "login", "value": "={{ $('Code: Decide Unlock').item.json.login }}", "type": "string"},
        {"id": "m3", "name": "action", "value": "unlock", "type": "string"},
        {"id": "m4", "name": "unlocked", "value": "yes", "type": "string"}]},
        "options": {}}, 2640, -260, ver=3.4)
    node("Mark Unlock Failed", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "f1", "name": "ticket_id", "value": "={{ $('Code: Decide Unlock').item.json.ticket_id }}", "type": "number"},
        {"id": "f2", "name": "login", "value": "={{ $('Code: Decide Unlock').item.json.login }}", "type": "string"},
        {"id": "f3", "name": "action", "value": "unlock", "type": "string"},
        {"id": "f4", "name": "unlocked", "value": "no", "type": "string"},
        {"id": "f5", "name": "error", "value": "={{ String($json.error || 'невідома помилка') }}", "type": "string"}]},
        "options": {}}, 2640, -120, ver=3.4)
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 4}, 2860, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 3080, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 3300, 0, credentials=cred, onError="continueRegularOutput")
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [], "value": {
            "ticket_id": "={{ $('Code: Build Report').item.json.ticket_id }}",
            "kind": "={{ $('Code: Build Report').item.json.kind }}",
            "logins": "={{ $('Code: Build Report').item.json.logins }}",
            "outcome": "={{ $json.error ? 'ERROR: ' + ($json.error.message || $json.error) : $('Code: Build Report').item.json.outcome }}",
            "dry_run": "={{ $('Code: Build Report').item.json.dry_run }}",
            "details": "={{ $('Code: Build Report').item.json.details }}",
            "at": "={{ $('Code: Build Report').item.json.at }}"},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True}
                   for c, t in [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"),
                                ("dry_run", "boolean"), ("details", "string"), ("at", "string")]]},
         "options": {}}, 3520, 0, ver=1.1)

    for a, b in [("Every 5 Min", "Config"), ("Config", "[Redmine] Get Locked IT Admins"), ("[Redmine] Get Locked IT Admins", "[Data Table] Get Processed"),
                 ("[Data Table] Get Processed", "[Redmine] Get New Tickets"), ("[Redmine] Get New Tickets", "Code: Parse Tickets"),
                 ("Code: Parse Tickets", "Route by Type"), ("Split Out Logins", "[Redmine] Find Locked User"),
                 ("[Redmine] Find Locked User", "Code: Decide Unlock"), ("Code: Decide Unlock", "Can Unlock Live?"),
                 ("[Redmine] Unlock User", "Mark Unlocked"), ("Merge Results", "Code: Build Report"),
                 ("Code: Build Report", "[Redmine] Update Ticket"), ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Route by Type", "Split Out Logins", 0)
    link("Route by Type", "Merge Results", 1, 2)
    link("Can Unlock Live?", "[Redmine] Unlock User", 0)
    link("Can Unlock Live?", "Merge Results", 1, 1)
    link("[Redmine] Unlock User", "Mark Unlock Failed", 1)
    link("Mark Unlocked", "Merge Results", 0, 0)
    link("Mark Unlock Failed", "Merge Results", 0, 3)
    return {"name": "Support Agent: Redmine Unlock", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred-id", default="<REDMINE_CRED>")
    ap.add_argument("--cred-name", default="Redmine <ENV> account")
    ap.add_argument("--table-id", default="<AUDIT_TABLE>")
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-redmine-unlock.json"))
    a = ap.parse_args()
    wf = build(a.cred_id, a.cred_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
