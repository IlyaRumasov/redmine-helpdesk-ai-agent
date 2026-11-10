"""Збирає workflows/support-agent-cvat-apply.json (флоу «Автоматизатор: CVAT Apply»).

Виконує дію з CVAT, яку підготував флоу CVAT (аудит: kind cvat_*, outcome awaiting_approval), після коментаря «Погоджую» від
учасника «IT Адміни» (не автора заявки). Розгортання йде через флоу 2 CVAT ChatOps (chat_space = ticket-<номер>, доступи
потім надсилає флоу 3 листами), вимкнення/увімкнення/видалення через інструмент Hetzner (видалення зі снепшотом).
Перед виконанням у аудит пишеться рядок applying, щоб наступний запуск не виконав дію вдруге.

Запуск: python tools/build_cvat_apply.py [--redmine-cred ID --redmine-name N --http-cred ID --http-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path
import build_cvat
import build_group_apply as BGA

ROOT = Path(__file__).resolve().parent.parent
HELPERS = BGA.HELPERS  # saluteOf, polite

MATCH_JS = BGA.MATCH_JS.replace("r.kind !== 'group_add'", "!String(r.kind).startsWith('cvat_') || r.kind === 'cvat_escalate'").replace(
    "plan: p, users: p.users,", "plan: p, kind: r.kind,")
DECIDE_JS = BGA.DECIDE_JS

CALLS_JS = r"""
const cfg = $('Config').first().json;
const out = [];
// після запису applying на вході рядки аудиту, тому план беремо з рішення Decide Apply
for (const { json: t } of $('Code: Decide Apply').all().filter(i => i.json.action === 'apply' && !i.json.dry_run)) {
  const p = t.plan, space = 'ticket-' + t.ticket_id;
  if (p.action === 'deploy') {
    out.push({ json: { ticket_id: t.ticket_id, plan: p, route: 'deploy', project_name: p.project, email: p.email, server_type: p.type, manager_username: p.login, chat_space: space } });
  } else if (p.action === 'close') {   // інструмент Hetzner просить підтвердження видалення: перший виклик питає, другий відповідає «так» (зі снепшотом)
    out.push({ json: { ticket_id: t.ticket_id, plan: p, route: 'tool', step: 'ask', query: 'delete_server ' + p.server, identifier: '', chat_space: space, chat_text: '' } });
    out.push({ json: { ticket_id: t.ticket_id, plan: p, route: 'tool', step: 'confirm', query: 'delete_server ' + p.server, identifier: '', chat_space: space, chat_text: 'так' } });
  } else {
    out.push({ json: { ticket_id: t.ticket_id, plan: p, route: 'tool', step: 'do', query: (p.action === 'pause' ? 'poweroff ' : 'poweron ') + p.server, identifier: '', chat_space: space, chat_text: '' } });
  }
}
return out;
"""

REPORT_JS = HELPERS + r"""
const calls = (() => { try { return $('Code: Build Calls').all(); } catch (e) { return []; } })();
const resOf = (c, k) => { for (const n of ['Execute: Deploy Flow', 'Execute: Hetzner Tool']) { try { const all = $(n).all(); const mine = calls.filter(x => x.json.route === (n.includes('Deploy') ? 'deploy' : 'tool')); const idx = mine.indexOf(c); if (idx >= 0 && all[idx]) return all[idx].json; } catch (e) { } } return null; };
let servers = null;
try { servers = $('[Hetzner] Get Servers').first().json.servers; } catch (e) { }
const rows = [], live = {};
for (const { json: j } of $input.all()) {
  if (!j.action) continue;                         // елементи після виконання не мають поля action: їх береться з calls
  const p = j.plan || {};
  let note, outcome, assign = false, details = j.reason || '';
  if (j.action === 'apply') { outcome = 'dry_applied'; details = p.server; note = `DRY RUN (змін не внесено). Агент виконав би: ${p.action} ${p.server}.`; }
  else if (j.action === 'cancel') { outcome = j.dry_run ? 'dry_cancelled' : 'cancelled'; note = `Агент: дію з CVAT скасовано (${details}). Якщо вона ще потрібна, створіть нову заявку.`; }
  else { outcome = j.dry_run ? 'dry_escalate' : 'escalate'; assign = !j.dry_run; note = `Агент: дію з CVAT не виконано. Причина: ${details}. Потрібна перевірка людиною.`; }
  rows.push({ ticket_id: j.ticket_id, kind: 'cvat_' + (p.action || 'escalate'), note, private: true, close: false, assign, logins: p.server || '', outcome, details, payload: JSON.stringify(p), dry_run: j.dry_run });
}
const apprOf = id => { try { const d = $('Code: Decide Apply').all().find(x => String(x.json.ticket_id) === String(id)); return d ? d.json.approver_id : undefined; } catch (e) { return undefined; } };
const byT = {};
for (const c of calls) (byT[c.json.ticket_id] = byT[c.json.ticket_id] || []).push(c);
for (const [tid, cs] of Object.entries(byT)) {
  const p = cs[0].json.plan, last = resOf(cs[cs.length - 1]) || {};
  const exists = Array.isArray(servers) ? servers.some(s => s.name === p.server) : null;
  const msg = String(last.message || last.output || last.error || '').slice(0, 300);
  let ok, uk, en, close = true, detail;
  if (p.action === 'deploy') {
    ok = exists === true; close = false; detail = ok ? `сервер ${p.server} створено, чекаємо встановлення` : 'сервер не створено';
    uk = `Розгортання CVAT запущено. Встановлення займає 10–15 хвилин, після нього на вашу пошту надійде лист з адресою та доступом менеджера проєкту.`;
    en = `The CVAT deployment has started. It takes 10–15 minutes; then you will get an email with the address and the project manager login.`;
  } else if (p.action === 'close') {
    ok = exists === false; detail = ok ? `сервер ${p.server} видалено (зі снепшотом)` : `сервер ще існує. ${msg}`;
    uk = `Сервер CVAT для проєкту видалено (перед цим зроблено знімок на випадок, якщо дані знадобляться).`;
    en = `The CVAT server for the project has been deleted (a snapshot was taken first in case the data is needed).`;
  } else if (p.action === 'pause') {
    ok = last.success === true && exists !== false; detail = ok ? `${p.server} вимкнено` : `не вдалося вимкнути. ${msg}`;
    uk = `Сервер CVAT вимкнено, дані збережено. Щоб увімкнути знову, створіть заявку «Відновлення CVAT».`;
    en = `The CVAT server is switched off and the data is kept. To switch it on again, create a "Restore CVAT" ticket.`;
  } else {
    ok = last.success === true && exists !== false; detail = ok ? `${p.server} увімкнено` : `не вдалося увімкнути. ${msg}`;
    uk = `Сервер CVAT увімкнено. Він може стати доступним через кілька хвилин, прошу перевірити.`;
    en = `The CVAT server is switched on. It can take a few minutes to become available; please check.`;
  }
  if (ok) rows.push({ ticket_id: Number(tid), kind: 'cvat_' + p.action, note: polite(uk, en, Number(tid)), private: false, close, assign: false, approver_id: apprOf(tid), logins: p.server, outcome: 'applied', details: detail, payload: JSON.stringify(p), dry_run: false });
  else rows.push({ ticket_id: Number(tid), kind: 'cvat_' + p.action, note: `Агент: дію ${p.action} для ${p.server} виконано не повністю. ${detail}. Потрібна перевірка людиною.`, private: true, close: false, assign: true, logins: p.server, outcome: 'escalate', details: detail, payload: JSON.stringify(p), dry_run: false });
}
return rows.map(r => ({ json: { ...r, at: new Date().toISOString() } }));
"""


def build(rm_id, rm_name, http_id, http_name, table_id, hz_id="<HETZNER_CRED>", hz_name="Hetzner Cloud",
          deploy_id="<DEPLOY_FLOW_ID>", tool_id="<HETZNER_TOOL_ID>"):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    hz = {"httpBearerAuth": {"id": hz_id, "name": hz_name}}
    nodes, conn = [], {}

    def node(name, type_, params, x, y, ver=1, **extra):
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        n.update(extra)
        nodes.append(n)

    def link(a, b, out=0, inp=0):
        m = conn.setdefault(a, {"main": []})["main"]
        while len(m) <= out:
            m.append([])
        m[out].append({"node": b, "type": "main", "index": inp})

    RM = "n8n-nodes-redmine.redmine"
    table = {"__rl": True, "mode": "id", "value": table_id}
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    schema = [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: CVAT Apply\n"
        "Кожні 5 хв перевіряє заявки, для яких флоу CVAT підготував дію (аудит: cvat_* / awaiting_approval).\n"
        "Після коментаря «Погоджую» від учасника «IT Адміни» (не автора) виконує: розгортання (флоу 2 CVAT ChatOps, доступи потім листами\n"
        "надсилає флоу 3), вимкнення, увімкнення або видалення зі снепшотом (інструмент Hetzner), відповідає авторові й закриває задачу.\n"
        "Перед виконанням пише в аудит рядок applying: повторно дія не запуститься. Закрита задача або 7 днів без підтвердження = скасовано.\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює."), "height": 240, "width": 700}, -200, -340)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "approve_words", "value": "погоджую,підтверджую", "type": "string"},
        {"id": "a3", "name": "pending_days", "value": 7, "type": "number"},
        {"id": "a4", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a5", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a6", "name": "redmine_url", "value": "<REDMINE_URL>", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Log", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("Code: Match Pending", "n8n-nodes-base.code", {"jsCode": MATCH_JS.strip()}, 660, 0, ver=2)
    node("[Redmine] Get Ticket History", RM, {"operation": "get", "issueId": "={{ $json.ticket_id }}", "include": ["journals"], "options": {}},
         880, 0, credentials=rmc)
    node("[Redmine] Get IT Admins", RM, {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"group_id": "={{ $('Config').first().json.admins_group_id }}", "status": "1"}, "options": {}}, 1100, 0,
         credentials=rmc, executeOnce=True, alwaysOutputData=True)
    node("Code: Decide Apply", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1320, 0, ver=2)
    node("Apply Now?", "n8n-nodes-base.if", {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c", "leftValue": "={{ $json.action === 'apply' && !$json.dry_run }}", "rightValue": "",
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}}, 1540, 0, ver=2.2)
    node("[Data Table] Log Applying", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [], "value": {
            "ticket_id": "={{ $json.ticket_id }}", "kind": "={{ $json.plan.kind || 'cvat_' + $json.plan.action }}", "logins": "={{ $json.plan.server }}",
            "outcome": "applying", "dry_run": False, "details": "={{ 'погоджено: ' + $json.approved_by }}", "at": "={{ new Date().toISOString() }}",
            "payload": "={{ JSON.stringify($json.plan) }}"}, "schema": schema}, "options": {}}, 1760, -120, ver=1.1)
    node("Code: Build Calls", "n8n-nodes-base.code", {"jsCode": CALLS_JS.strip()}, 1980, -120, ver=2,
         )
    node("Route: Action", "n8n-nodes-base.switch", {"rules": {"values": [
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2}, "combinator": "and", "conditions": [
            {"id": "r1", "leftValue": "={{ $json.route }}", "rightValue": "deploy", "operator": {"type": "string", "operation": "equals"}}]}, "renameOutput": True, "outputKey": "deploy"},
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2}, "combinator": "and", "conditions": [
            {"id": "r2", "leftValue": "={{ $json.route }}", "rightValue": "tool", "operator": {"type": "string", "operation": "equals"}}]}, "renameOutput": True, "outputKey": "tool"}]},
        "options": {}}, 2200, -120, ver=3.2)
    node("Execute: Deploy Flow", "n8n-nodes-base.executeWorkflow", {"workflowId": {"__rl": True, "mode": "id", "value": deploy_id},
         "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [], "attemptToConvertTypes": False, "convertFieldsToString": True},
         "mode": "each", "options": {}}, 2420, -220, ver=1.2, onError="continueRegularOutput")
    node("Execute: Hetzner Tool", "n8n-nodes-base.executeWorkflow", {"workflowId": {"__rl": True, "mode": "id", "value": tool_id},
         "workflowInputs": {"mappingMode": "defineBelow", "value": {k: "={{ $json.%s }}" % k for k in ("query", "identifier", "chat_space", "chat_text")},
                            "matchingColumns": [], "schema": [{"id": k, "displayName": k, "required": False, "defaultMatch": False, "display": True, "type": "string", "canBeUsedToMatch": True}
                                                              for k in ("query", "identifier", "chat_space", "chat_text")], "attemptToConvertTypes": False, "convertFieldsToString": True},
         "mode": "each", "options": {}}, 2420, 0, ver=1.2, onError="continueRegularOutput")
    node("Merge Calls", "n8n-nodes-base.merge", {"numberInputs": 2}, 2640, -120, ver=3.2)
    node("[Hetzner] Get Servers", "n8n-nodes-base.httpRequest", {"url": "https://api.hetzner.cloud/v1/servers", "authentication": "genericCredentialType",
         "genericAuthType": "httpBearerAuth", "sendQuery": True, "queryParameters": {"parameters": [{"name": "per_page", "value": "50"}]}, "options": {}},
         2860, -120, ver=4.2, credentials=hz, executeOnce=True, onError="continueRegularOutput")
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 2}, 3080, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 3300, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"customFields": {"field": [{"id": "92", "value": "={{ $json.close ? 'Создание нового сервера/сервиса/ресурса' : '' }}"}]},
                              "status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : ($json.approver_id || undefined) }}"},
         "options": {}}, 3520, 0, credentials=rmc, onError="continueRegularOutput")
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols}, "schema": schema}, "options": {}}, 3740, 0, ver=1.1)
    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Log"), ("[Data Table] Get Log", "Code: Match Pending"),
                 ("Code: Match Pending", "[Redmine] Get Ticket History"), ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins"),
                 ("[Redmine] Get IT Admins", "Code: Decide Apply"), ("Code: Decide Apply", "Apply Now?"),
                 ("[Data Table] Log Applying", "Code: Build Calls"), ("Code: Build Calls", "Route: Action"),
                 ("[Hetzner] Get Servers", "Merge Results"),
                 ("Merge Results", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"), ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Apply Now?", "[Data Table] Log Applying", 0)
    link("Apply Now?", "Merge Results", 1, 1)
    link("Route: Action", "Execute: Deploy Flow", 0)
    link("Route: Action", "Execute: Hetzner Tool", 1)
    link("Execute: Deploy Flow", "Merge Calls", 0, 0)
    link("Execute: Hetzner Tool", "Merge Calls", 0, 1)
    link("Merge Calls", "[Hetzner] Get Servers")
    for n in nodes:  # повторюємо лише читання
        if n["name"] in ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins", "[Hetzner] Get Servers"):
            n.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})
    return {"name": "Автоматизатор: CVAT Apply", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("http-cred", "<REDMINE_HTTP_CRED>"),
                 ("http-name", "Redmine HTTP header auth"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-cvat-apply.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.http_cred, a.http_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
