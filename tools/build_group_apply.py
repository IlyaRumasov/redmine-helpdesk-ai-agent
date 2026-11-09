"""Збирає workflows/support-agent-redmine-group-apply.json (флоу «Автоматизатор: Redmine Group Apply»).

Виконує додавання людей до групи Redmine, яке підготував флоу Redmine Group (аудит: kind group_add, outcome awaiting_approval),
після коментаря «Погоджую» від учасника «IT Адміни» (не автора заявки): POST /groups/<id>/users.json, потім перевірка членства,
відповідь авторові і закриття задачі.

Запуск: python tools/build_group_apply.py [--redmine-cred ID --redmine-name N --http-cred ID --http-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path
import build_guest

ROOT = Path(__file__).resolve().parent.parent
HELPERS = build_guest.REPORT_JS.split("return $input.all()")[0]  # saluteOf, polite

MATCH_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
// у DRY RUN дивимось на найновіший запис (і сухий, і бойовий), у бойовому режимі лише бойові
const last = {}, newest = {};
for (const { json: r } of $input.all()) {
  if (!r.ticket_id) continue;
  if (!newest[r.ticket_id] || String(r.at) > String(newest[r.ticket_id].at)) newest[r.ticket_id] = r;
  if (r.dry_run === true) continue;
  if (!last[r.ticket_id] || String(r.at) > String(last[r.ticket_id].at)) last[r.ticket_id] = r;
}
const out = [];
for (const r of Object.values(dry ? newest : last)) {
  if (r.kind !== 'group_add' || r.outcome !== 'awaiting_approval') continue;
  let p;
  try { p = JSON.parse(r.payload); } catch (e) { continue; }
  out.push({ json: { ticket_id: r.ticket_id, plan: p, users: p.users, prepared_at: r.at, dry_run: dry } });
}
return out;
"""

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const words = String(cfg.approve_words).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const maxAge = Number(cfg.pending_days) * 86400000;
const admins = new Set((($('[Redmine] Get IT Admins').first().json.users) || []).map(u => String(u.id)));
const pend = $('Code: Match Pending').all();
const hist = $('[Redmine] Get Ticket History').all();
const out = [];
pend.forEach(({ json: t }, i) => {
  const r = { ...t, action: 'escalate', reason: '' };
  const h = hist[i] && hist[i].json || {};
  const issue = h.issue || h;
  if (!issue.id) { out.push({ json: { ...r, reason: 'не вдалося прочитати заявку' } }); return; }
  const author = String(issue.author && issue.author.id);
  if ((issue.status && issue.status.is_closed) || ['17', '21'].includes(String(issue.status && issue.status.id))) {
    out.push({ json: { ...r, action: 'cancel', reason: 'задачу закрито без підтвердження' } }); return;
  }
  const isWord = n => words.includes(String(n || '').trim().toLowerCase().replace(/[.!\s]+$/, ''));
  const notes = (issue.journals || []).filter(x => String(x.created_on) > String(t.prepared_at) && isWord(x.notes));
  const ok = notes.filter(x => admins.has(String(x.user && x.user.id)) && String(x.user.id) !== author);
  if (!ok.length) {
    if (notes.length) { out.push({ json: { ...r, reason: `підтвердження написав ${notes[0].user.name}, але він не з «IT Адміни» або є автором заявки` } }); return; }
    if (Date.now() - Date.parse(t.prepared_at) > maxAge) { out.push({ json: { ...r, action: 'cancel', reason: `немає підтвердження понад ${cfg.pending_days} днів` } }); return; }
    return;  // ще чекаємо
  }
  out.push({ json: { ...r, action: 'apply', approved_by: ok[0].user.name } });
});
return out;
"""

REPORT_JS = HELPERS + r"""
const cfg = $('Config').first().json;
const split = (() => { try { return $('Split Out Users').all(); } catch (e) { return []; } })();
const post = (() => { try { return $('[Redmine] Add To Group').all(); } catch (e) { return []; } })();
const live = {}, out = [];
let k = 0;
for (const { json: j } of $input.all()) {
  if (j.action) { out.push(j); continue; }   // не бойове додавання: сухий запуск, скасування, ескалація
  const s = split[k].json, p = post[k].json; k++;
  const member = ((((j.body || {}).user) || {}).groups || []).some(x => String(x.id) === String(cfg.group_id));
  const t = live[s.ticket_id] = live[s.ticket_id] || { ...s, ok: [], bad: [] };
  if (member) t.ok.push(s.users.name); else t.bad.push(`${s.users.name} (код ${p.statusCode})`);
}
const rows = out.map(j => {
  const p = j.plan || {}, names = (p.users || []).map(u => u.name).join(', ');
  let note, outcome, close = false, assign = false, details = j.reason || '';
  if (j.action === 'apply') {  // сухий запуск
    outcome = 'dry_applied'; details = names;
    note = `DRY RUN (змін не внесено). Агент додав би до групи «${p.group_name}»: ${names}.`;
  } else if (j.action === 'cancel') {
    outcome = j.dry_run ? 'dry_cancelled' : 'cancelled';
    note = `Агент: додавання до групи скасовано (${details}). Якщо воно ще потрібне, створіть нову заявку.`;
  } else {
    outcome = j.dry_run ? 'dry_escalate' : 'escalate'; assign = !j.dry_run;
    note = `Агент: додавання до групи не виконано. Причина: ${details}. Потрібна перевірка людиною.`;
  }
  return { ticket_id: j.ticket_id, note, private: true, close, assign, logins: names, outcome, details, payload: JSON.stringify(p), dry_run: j.dry_run };
});
for (const t of Object.values(live)) {
  const p = t.plan, ok = t.ok.join(', ');
  let note, outcome, close = false, assign = false, details, pub = false;
  if (!t.bad.length) {
    outcome = 'applied'; close = true; pub = true; details = `додано до групи: ${ok}`;
    note = polite(`${t.ok.length > 1 ? 'Користувачів' : 'Користувача'} ${ok} додано до групи «${p.group_name}». Проєкти за замовчуванням мають з'явитися після повторного входу в Redmine (вийдіть і зайдіть знову).`,
      `${ok} ${t.ok.length > 1 ? 'have' : 'has'} been added to the "${p.group_name}" group. The default projects should appear after you sign out of Redmine and sign in again.`, t.ticket_id);
  } else {
    outcome = 'escalate'; assign = true;
    details = `не вдалося додати: ${t.bad.join(', ')}` + (ok ? `; додано: ${ok}` : '');
    note = `Агент: додавання до групи виконано не повністю. ${details}. Потрібна перевірка людиною.`;
  }
  rows.push({ ticket_id: t.ticket_id, note, private: !pub, close, assign, logins: t.users ? t.users.mail : '', outcome, details, payload: JSON.stringify(p), dry_run: false });
}
return rows.map(r => ({ json: { ...r, kind: 'group_add', at: new Date().toISOString() } }));
"""


def build(rm_id, rm_name, http_id, http_name, table_id):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    hc = {"httpHeaderAuth": {"id": http_id, "name": http_name}}
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
    BASE = "$('Config').first().json.redmine_url"
    http = lambda method, url: {"method": method, "url": url, "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                                "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Redmine Group Apply\n"
        "Кожні 5 хв перевіряє заявки, для яких флоу Redmine Group підготував додавання до групи (аудит: group_add / awaiting_approval).\n"
        "Після коментаря «Погоджую» від учасника «IT Адміни» (не автора) додає людей до групи, перевіряє членство,\n"
        "відповідає авторові й закриває задачу. Закрита задача або 7 днів без підтвердження = скасовано.\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює."), "height": 200, "width": 640}, -200, -300)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "approve_words", "value": "погоджую,підтверджую", "type": "string"},
        {"id": "a3", "name": "pending_days", "value": 7, "type": "number"},
        {"id": "a4", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a5", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a6", "name": "redmine_url", "value": "<REDMINE_URL>", "type": "string"},
        {"id": "a7", "name": "group_id", "value": "41", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
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
    node("Split Out Users", "n8n-nodes-base.splitOut", {"fieldToSplitOut": "users", "include": "allOtherFields", "options": {}}, 1760, -120, ver=1)
    node("[Redmine] Add To Group", "n8n-nodes-base.httpRequest", {**http("POST", "={{ " + BASE + " + '/groups/' + $('Config').first().json.group_id + '/users.json' }}"),
         "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ user_id: $json.users.id }) }}"},
         1980, -120, ver=4.2, credentials=hc, onError="continueRegularOutput")
    node("[Redmine] Verify Membership", "n8n-nodes-base.httpRequest", http("GET",
         "={{ " + BASE + " + '/users/' + $('Split Out Users').item.json.users.id + '.json?include=groups' }}"),
         2200, -120, ver=4.2, credentials=hc, onError="continueRegularOutput")
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 2}, 2420, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 2640, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"customFields": {"field": [{"id": "92", "value": "={{ $json.close ? 'Выдача/передача доступа' : '' }}"}]},
                              "status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 2860, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 3080, 0, ver=1.1)
    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Log"), ("[Data Table] Get Log", "Code: Match Pending"),
                 ("Code: Match Pending", "[Redmine] Get Ticket History"), ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins"),
                 ("[Redmine] Get IT Admins", "Code: Decide Apply"), ("Code: Decide Apply", "Apply Now?"),
                 ("Split Out Users", "[Redmine] Add To Group"), ("[Redmine] Add To Group", "[Redmine] Verify Membership"),
                 ("Merge Results", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"),
                 ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Apply Now?", "Split Out Users", 0)
    link("Apply Now?", "Merge Results", 1, 1)
    link("[Redmine] Verify Membership", "Merge Results", 0, 0)
    for n in nodes:  # повторюємо лише читання; запис (додавання до групи, коментар) не повторюємо
        if n["name"] in ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins", "[Redmine] Verify Membership"):
            n.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})
    return {"name": "Автоматизатор: Redmine Group Apply", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("http-cred", "<REDMINE_HTTP_CRED>"),
                 ("http-name", "Redmine HTTP header auth"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-redmine-group-apply.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.http_cred, a.http_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
