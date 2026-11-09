"""Збирає workflows/support-agent-cvat.json (флоу «Автоматизатор: CVAT»).

Заявки HelpDesk про CVAT: «розгорнути / New DA tool installation», «відновити», «вимкнути / поставити на паузу», «закрити / видалити».
Агент визначає дію й проєкт, знаходить сервер cvat-* у Hetzner (лише читання), перевіряє, що дія можлива, і пише приватну
нотатку з планом. Виконує флоу CVAT Apply після коментаря «Погоджую» від учасника «IT Адміни» (не автора).
Аудит: kind cvat_deploy|cvat_resume|cvat_pause|cvat_close, outcome awaiting_approval, payload = JSON плану.
Заявки про несправності, оновлення, S3, паролі та консультації не чіпає: їх обробляють люди.

Запуск: python tools/build_cvat.py [--redmine-cred ID --redmine-name N --http-cred ID --http-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PARSE_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const seen = new Set($('[Data Table] Get Processed').all()
  .filter(i => i.json.kind !== 'dispatch' && (dry || i.json.dry_run === false)).map(i => String(i.json.ticket_id)));
const out = [];
for (const t of ($input.first().json.issues || [])) {
  if ((t.assigned_to && String(t.assigned_to.id) !== String(cfg.admins_group_id)) || seen.has(String(t.id))) continue;
  const subj = String(t.subject || '').toLowerCase(), desc = String(t.description || '');
  if (!/cvat/.test(subj + ' ' + desc.toLowerCase())) continue;
  // несправності, оновлення, S3, паролі, консультації: тільки люди
  if (/(не завантаж|проблем|не працю|помилк|error|502|оновл|update|upgrade|sam2|aws|s3|bucket|парол|password|консульт|disk|диск|дост[уі]п)/.test(subj)) continue;
  const acts = [];
  if (/(da tool installation|розгор|встановл|створенн|створити)/.test(subj) || /^\s*tool:\s*cvat/i.test(desc)) acts.push('deploy');
  if (/(відновл|restore|повернут|увімкн|запуст)/.test(subj)) acts.push('resume');
  if (/(пауз|вимкн)/.test(subj)) acts.push('pause');
  if (/(закрит|видал|завершен)/.test(subj)) acts.push('close');
  const base = { ticket_id: t.id, author_id: t.author && t.author.id, action: acts[0] || '', project: '', reason: '' };
  if (acts.length !== 1) { out.push({ ...base, action: 'escalate', reason: acts.length ? 'в заявці кілька різних дій' : 'не зрозуміла дія' }); continue; }
  let p = (desc.match(/project:\s*([^\n]+?)\s*(?:data\s*set:|super\s*user|$)/i) || [])[1] || '';
  if (!p) p = (`${t.subject} ${desc}`.match(/cvat-([a-z0-9-]+)\.examples?\.(?:tech|com)/i) || [])[1] || '';
  if (!p) p = (`${t.subject} ${desc}`.match(/\bcvat-([a-z0-9][a-z0-9-]*)/i) || [])[1] || '';
  if (!p) p = (String(t.subject).match(/\(([^)]+)\)/) || [])[1] || '';
  if (!p) p = (String(t.subject).match(/(?:cvat|дел|tool)\s*[-:]?\s*([A-Za-z0-9-]+)\s*$/i) || [])[1] || '';
  out.push({ ...base, project: p.trim() });
}
return out.map(json => ({ json }));
"""

PLAN_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const items = $('Code: Parse Tickets').all();
const authors = $('[Redmine] Get Author').all();
const list = (($('[Hetzner] List Servers').first().json || {}).servers);
if (!Array.isArray(list)) return [];                 // Hetzner недоступний: нічого не логуємо, спробуємо на наступному запуску
const servers = list.filter(s => /^cvat-/.test(s.name));
const norm = s => String(s || '').toLowerCase().replace(/^cvat-/, '').replace(/[^a-z0-9]/g, '');
const clean = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
return items.map(({ json: t }, i) => {
  const r = { ticket_id: t.ticket_id, dry_run: dry, action: t.action, plan: null, reason: '' };
  if (t.action === 'escalate') return { json: { ...r, reason: t.reason } };
  const u = ((authors[i].json.body || {}).user) || {};
  const proj = clean(t.project);
  if (!proj) return { json: { ...r, action: 'escalate', reason: 'не вдалося визначити назву проєкту (потрібна латиниця)' } };
  const found = servers.filter(s => norm(s.name) === norm(proj));
  if (t.action === 'deploy') {
    if (found.length) return { json: { ...r, action: 'escalate', reason: `сервер ${found[0].name} уже існує (${found[0].status})` } };
    if (!/^[A-Za-z0-9_.-]{2,32}$/.test(u.login || '')) return { json: { ...r, action: 'escalate', reason: 'логін автора заявки не підходить для CVAT (потрібні латиниця, цифри, _ . -)' } };
    if (!u.mail) return { json: { ...r, action: 'escalate', reason: 'у автора заявки немає пошти в Redmine' } };
    return { json: { ...r, plan: { action: 'deploy', server: 'cvat-' + proj, project: proj, type: cfg.server_type, login: u.login, email: String(u.mail).toLowerCase() } } };
  }
  if (found.length !== 1) return { json: { ...r, action: 'escalate', reason: found.length ? `назві «${t.project}» відповідає кілька серверів: ${found.map(s => s.name).join(', ')}` : `сервера для «${t.project}» немає в Hetzner (відновлення зі снепшота, якщо потрібне, робить людина)` } };
  const s = found[0];
  if (t.action === 'pause' && s.status !== 'running') return { json: { ...r, action: 'escalate', reason: `${s.name} уже не працює (статус ${s.status})` } };
  if (t.action === 'resume' && s.status !== 'off') return { json: { ...r, action: 'escalate', reason: `${s.name} не вимкнено (статус ${s.status})` } };
  if (t.action === 'close' && s.protection && s.protection.delete) return { json: { ...r, action: 'escalate', reason: `${s.name} захищено від видалення в Hetzner` } };
  return { json: { ...r, plan: { action: t.action, server: s.name, project: proj, server_id: s.id, status: s.status } } };
});
"""

REPORT_JS = r"""
const WHAT = { deploy: 'розгорнути новий CVAT', resume: 'увімкнути вимкнений сервер', pause: 'вимкнути сервер (дані зберігаються, його можна увімкнути знову)', close: 'ВИДАЛИТИ сервер (спершу автоматично зробити снепшот)' };
return $input.all().map(({ json: j }) => {
  const p = j.plan, ok = j.action !== 'escalate' && p;
  let note, outcome, payload = '', details, kind = 'cvat_' + (p ? p.action : (j.action === 'escalate' ? 'escalate' : j.action));
  if (ok) {
    outcome = 'awaiting_approval'; payload = JSON.stringify(p);
    details = `${p.action}: ${p.server}`;
    const extra = p.action === 'deploy' ? ` Тариф ${p.type}, менеджер проєкту ${p.login}. Після встановлення (10–15 хв) доступи прийдуть листом: автору заявки (адреса й доступ менеджера проєкту), адміністраторам (повний набір).` : '';
    note = `Агент підготував: ${WHAT[p.action]} ${p.server}.${extra} Щоб виконати, учасник «IT Адміни» (не автор заявки) пише в задачі коментар «Погоджую». Через 7 днів без підтвердження дію скасовано.`;
    if (j.dry_run) note = `DRY RUN (змін не внесено). Агент підготував би: ${WHAT[p.action]} ${p.server}.${extra} І чекав би «Погоджую».`;
  } else {
    outcome = 'escalate'; details = j.reason;
    note = (j.dry_run ? 'DRY RUN (змін не внесено). Агент передав би задачу адміністраторам.' : 'Агент: автоматично не обробляю.') + ` Причина: ${j.reason}.` + (j.dry_run ? '' : ' Передано адміністраторам.');
  }
  return { json: { ticket_id: j.ticket_id, kind, note, private: true, assign: !j.dry_run && !ok, logins: p ? p.server : '', outcome, details, payload, dry_run: j.dry_run, at: new Date().toISOString() } };
});
"""


def build(rm_id, rm_name, http_id, http_name, table_id, hz_id="<HETZNER_CRED>", hz_name="Hetzner Cloud"):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    hc = {"httpHeaderAuth": {"id": http_id, "name": http_name}}
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
    cfgv = lambda k: "={{ $('Config').first().json.%s }}" % k
    table = {"__rl": True, "mode": "id", "value": table_id}
    BASE = "$('Config').first().json.redmine_url"

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: CVAT\n"
        "Кожні 5 хв бере нові нерозподілені задачі HelpDesk про CVAT: розгорнути, відновити, вимкнути, закрити.\n"
        "Знаходить сервер cvat-* у Hetzner (лише читання), перевіряє, що дія можлива, і пише приватну нотатку з планом.\n"
        "Виконує флоу CVAT Apply після коментаря «Погоджую» від учасника «IT Адміни» (не автора).\n"
        "Несправності, оновлення, S3, паролі й консультації не чіпає.\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює, лише нотатка й аудит."), "height": 220, "width": 640}, -200, -320)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "tracker_id", "value": "<TRACKER_ID>", "type": "string"},
        {"id": "a4", "name": "redmine_url", "value": "<REDMINE_URL>", "type": "string"},
        {"id": "a5", "name": "server_type", "value": "cpx42", "type": "string"},
        {"id": "a6", "name": "admins_group_id", "value": "35", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Processed", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get New Tickets", RM, {"operation": "getAll", "returnAll": True, "filters": {
         "project_id": cfgv("project_id"), "tracker_id": cfgv("tracker_id"), "status_id": "1"}, "options": {}}, 660, 0, credentials=rmc)
    node("Code: Parse Tickets", "n8n-nodes-base.code", {"jsCode": PARSE_JS.strip()}, 880, 0, ver=2)
    node("[Redmine] Get Author", "n8n-nodes-base.httpRequest", {"url": "={{ " + BASE + " + '/users/' + $json.author_id + '.json' }}",
         "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
         "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}, 1100, 0, ver=4.2, credentials=hc, onError="continueRegularOutput")
    node("[Hetzner] List Servers", "n8n-nodes-base.httpRequest", {"url": "https://api.hetzner.cloud/v1/servers", "authentication": "genericCredentialType",
         "genericAuthType": "httpBearerAuth", "sendQuery": True, "queryParameters": {"parameters": [{"name": "per_page", "value": "50"}]}, "options": {}},
         1320, 0, ver=4.2, credentials=hz, executeOnce=True, onError="continueRegularOutput")
    node("Code: Plan", "n8n-nodes-base.code", {"jsCode": PLAN_JS.strip()}, 1540, 0, ver=2)
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
                 ("[Redmine] Get New Tickets", "Code: Parse Tickets"), ("Code: Parse Tickets", "[Redmine] Get Author"),
                 ("[Redmine] Get Author", "[Hetzner] List Servers"), ("[Hetzner] List Servers", "Code: Plan"), ("Code: Plan", "Code: Build Report"),
                 ("Code: Build Report", "[Redmine] Update Ticket"), ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    for n in nodes:  # повторюємо лише читання; запис (коментар, аудит) не повторюємо
        if n["name"] in ("[Redmine] Get New Tickets", "[Redmine] Get Author", "[Hetzner] List Servers"):
            n.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})
    return {"name": "Автоматизатор: CVAT", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("http-cred", "<REDMINE_HTTP_CRED>"),
                 ("http-name", "Redmine HTTP header auth"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-cvat.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.http_cred, a.http_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
