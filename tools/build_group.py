"""Збирає workflows/support-agent-redmine-group.json (флоу «Автоматизатор: Redmine Group»).

Заявки «додайте до проєкту/групи (за замовчуванням група «Сотрудники»)»: агент знаходить людей у Redmine за робочою поштою,
перевіряє, що вони активні й ще не в групі, і пише приватну нотатку з планом. Виконує флоу Redmine Group Apply після
коментаря «Погоджую» від учасника «IT Адміни» (не автора). Аудит: kind group_add, outcome awaiting_approval, payload = JSON плану.

Запуск: python tools/build_group.py [--redmine-cred ID --redmine-name N --http-cred ID --http-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path
import build_guest

ROOT = Path(__file__).resolve().parent.parent
SALUTE = build_guest.PARSE_JS.split("const cfg = $('Config')")[0]  # saluteOf
HELPERS = build_guest.REPORT_JS.split("return $input.all()")[0]    # saluteOf, polite

PARSE_JS = SALUTE + r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const seen = new Set($('[Data Table] Get Processed').all()
  .filter(i => i.json.kind !== 'dispatch' && (dry || i.json.dry_run === false)).map(i => String(i.json.ticket_id)));
const out = [];
for (const t of ($input.first().json.issues || [])) {
  if ((t.assigned_to && String(t.assigned_to.id) !== String(cfg.admins_group_id)) || seen.has(String(t.id))) continue;
  const text = `${t.subject}\n${t.description || ''}`.toLowerCase();
  // чужі теми (Google, 2FA, паролі, розблокування) не чіпаємо
  if (/(google|гугл|gmail|2fa|гостьов|парол|розблок|разблок|unlock)/.test(text)) continue;
  const wants = /(доступ|призначи|додат|додай|надат|надайте|assign|add|access)/.test(text) && /(проєкт|проект|project|сотрудник|співробітник|груп)/.test(text);
  const mails = [...new Set(text.match(/[\w.+-]+@(?:example|example)\.com/g) || [])];
  if (!wants || !mails.length) continue;
  const base = { ticket_id: t.id, salute: saluteOf(t), email: null, reason: '' };
  if (mails.length > Number(cfg.max_users)) out.push({ ...base, kind: 'escalate', reason: `забагато адрес у заявці (${mails.length}), максимум ${cfg.max_users}` });
  else for (const m of mails) out.push({ ...base, kind: 'check', email: m });
}
return out.map(json => ({ json }));
"""

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const items = $('Code: Parse Tickets').all();
const found = $('[Redmine] Find User').all();
const groups = $input.all();
return items.map(({ json: t }, i) => {
  const r = { ...t, dry_run: dry, action: 'skip' };
  if (t.kind === 'escalate') return { json: { ...r, action: 'escalate' } };
  const u = (((found[i].json.body || {}).users) || []).find(x => String(x.mail || '').toLowerCase() === t.email);
  if (!u) return { json: { ...r, reason: `${t.email}: не знайдено серед активних у Redmine (людина могла ще жодного разу не входити під корпоративним обліковим записом)` } };
  const full = (groups[i].json.body || {}).user || {};
  const name = `${u.lastname || ''} ${u.firstname || ''}`.trim();
  Object.assign(r, { user_id: u.id, name });
  if (u.admin) return { json: { ...r, reason: `${name}: адміністратор Redmine, групу змінює тільки людина` } };
  if ((full.groups || []).some(g => String(g.id) === String(cfg.group_id))) return { json: { ...r, action: 'already', reason: `${name}: уже в групі «${cfg.group_name}»` } };
  return { json: { ...r, action: 'add' } };
});
"""

REPORT_JS = HELPERS + r"""
const cfg = $('Config').first().json;
const g = {};
for (const { json: j } of $input.all()) {
  const t = g[j.ticket_id] = g[j.ticket_id] || { id: j.ticket_id, add: [], problems: [], dry: j.dry_run };
  if (j.action === 'add') t.add.push({ id: j.user_id, name: j.name, mail: j.email });
  else t.problems.push(j.reason || 'невідома причина');
}
return Object.values(g).map(t => {
  const ok = t.add.length && !t.problems.length;
  const who = t.add.map(u => `${u.name} (${u.mail})`).join(', ');
  let note, outcome, payload = '', details;
  if (ok) {
    outcome = 'awaiting_approval';
    payload = JSON.stringify({ group_id: cfg.group_id, group_name: cfg.group_name, users: t.add });
    details = who;
    note = `Агент підготував додавання до групи «${cfg.group_name}» (проєкти за замовчуванням): ${who}. Щоб виконати, учасник «IT Адміни» (не автор заявки) пише в задачі коментар «Погоджую». Через 7 днів без підтвердження дію скасовано.`;
  } else {
    outcome = 'escalate';
    details = t.problems.join('; ') + (t.add.length ? `; можна було б додати: ${who}` : '');
    note = `Агент: автоматично не обробляю. Причина: ${details}. Передано адміністраторам.`;
  }
  if (t.dry) note = `DRY RUN (змін не внесено). ` + (ok ? `Агент підготував би додавання до групи «${cfg.group_name}»: ${who}, і чекав би «Погоджую».` : `Агент передав би задачу адміністраторам. Причина: ${details}.`);
  return { json: { ticket_id: t.id, kind: 'group_add', note, private: true, assign: !t.dry, logins: t.add.map(u => u.mail).join(','),
                   outcome, details, payload, dry_run: t.dry, at: new Date().toISOString() } };
});
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
    cfgv = lambda k: "={{ $('Config').first().json.%s }}" % k
    table = {"__rl": True, "mode": "id", "value": table_id}
    http = lambda url: {"url": url, "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                        "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}
    BASE = "$('Config').first().json.redmine_url"

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Redmine Group\n"
        "Кожні 5 хв бере нові нерозподілені задачі HelpDesk «додайте до проєкту/групи» з робочими адресами.\n"
        "Знаходить людей у Redmine, перевіряє (активний, не адмін, ще не в групі) і пише приватну нотатку з планом.\n"
        "Додає в групу флоу Redmine Group Apply після коментаря «Погоджую» від учасника «IT Адміни» (не автора).\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює в групах, лише нотатка й аудит."), "height": 200, "width": 640}, -200, -300)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "tracker_id", "value": "<TRACKER_ID>", "type": "string"},
        {"id": "a4", "name": "redmine_url", "value": "<REDMINE_URL>", "type": "string"},
        {"id": "a5", "name": "group_id", "value": "41", "type": "string"},
        {"id": "a6", "name": "group_name", "value": "Сотрудники", "type": "string"},
        {"id": "a7", "name": "max_users", "value": 5, "type": "number"},
        {"id": "a8", "name": "admins_group_id", "value": "35", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Processed", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get New Tickets", RM, {"operation": "getAll", "returnAll": True, "filters": {
         "project_id": cfgv("project_id"), "tracker_id": cfgv("tracker_id"), "status_id": "1"}, "options": {}}, 660, 0, credentials=rmc)
    node("Code: Parse Tickets", "n8n-nodes-base.code", {"jsCode": PARSE_JS.strip()}, 880, 0, ver=2)
    node("[Redmine] Find User", "n8n-nodes-base.httpRequest", http(
         "={{ " + BASE + " + '/users.json?limit=5&name=' + encodeURIComponent($json.email || '__none__') }}"),
         1100, 0, ver=4.2, credentials=hc, onError="continueRegularOutput")
    node("[Redmine] Get User Groups", "n8n-nodes-base.httpRequest", http(
         "={{ " + BASE + " + '/users/' + ((((($json.body || {}).users) || []).find(x => String(x.mail || '').toLowerCase() === $('Code: Parse Tickets').item.json.email) || {}).id || 0) + '.json?include=groups' }}"),
         1320, 0, ver=4.2, credentials=hc, onError="continueRegularOutput")
    node("Code: Decide Group Action", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1540, 0, ver=2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 1760, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 1980, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 2200, 0, ver=1.1)
    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Processed"), ("[Data Table] Get Processed", "[Redmine] Get New Tickets"),
                 ("[Redmine] Get New Tickets", "Code: Parse Tickets"), ("Code: Parse Tickets", "[Redmine] Find User"),
                 ("[Redmine] Find User", "[Redmine] Get User Groups"), ("[Redmine] Get User Groups", "Code: Decide Group Action"),
                 ("Code: Decide Group Action", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"),
                 ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    for n in nodes:  # разовий збій мережі/DNS не має ставати помилкою: повторюємо лише читання (запис не повторюємо, щоб не дублювати коментар)
        if n["name"] in ("[Redmine] Get New Tickets", "[Redmine] Find User", "[Redmine] Get User Groups"):
            n.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})
    return {"name": "Автоматизатор: Redmine Group", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("http-cred", "<REDMINE_HTTP_CRED>"),
                 ("http-name", "Redmine HTTP header auth"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-redmine-group.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.http_cred, a.http_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
