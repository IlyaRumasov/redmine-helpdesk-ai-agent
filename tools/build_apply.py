"""Збирає workflows/support-agent-apply-approved.json (флоу «Автоматизатор: Apply Approved Changes»).

Виконує зміни, які агент підготував і поставив на «Needs approve» (зараз: зміна особистої пошти, postOfficeBox),
лише після коментаря «Погоджую» від учасника групи «IT Адміни» (не автора заявки).
Перед зміною ще раз звіряє AD: той самий обліковий запис, у дозволеному OU, пошта не змінилась після підготовки.

Запуск: python tools/build_apply.py [--redmine-cred ID --redmine-name NAME --ldap-cred ID --ldap-name NAME --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = "https://reset-password.example.com/"

MATCH_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
// останній бойовий запис аудиту по кожній заявці; беремо ті, що чекають на підтвердження
// У DRY RUN дивимось на найновіший запис (і бойовий, і сухий): сухий «підготував би» теж чекає підтвердження,
// а сухий «виконав би» вже новіший за нього, тому повторно заявку не беремо. У бойовому режимі лише бойові записи.
const last = {}, newest = {};
for (const { json: r } of $input.all()) {
  if (!r.ticket_id) continue;
  if (!newest[r.ticket_id] || String(r.at) > String(newest[r.ticket_id].at)) newest[r.ticket_id] = r;
  if (r.dry_run === true) continue;
  if (!last[r.ticket_id] || String(r.at) > String(last[r.ticket_id].at)) last[r.ticket_id] = r;
}
const out = [];
for (const r of Object.values(dry ? newest : last)) {
  if (r.outcome !== 'awaiting_approval' || r.kind !== 'set_mail') continue;  // гостьові дії підтверджує флоу Google Guest Apply
  let p;
  try { p = JSON.parse(r.payload); } catch (e) { continue; }
  out.push({ json: { ticket_id: r.ticket_id, kind: r.kind, plan: p, prepared_at: r.at, dry_run: dry } });
}
// Слоти для пошуку в AD (нода LDAP не приймає фільтр цілком із виразу): усі логіни, до 16, зайві = __none__
const SL = [...new Set(out.map(x => x.json.plan && x.json.plan.login).filter(Boolean))].map(x => String(x).replace(/[*()\\\u0000]/g, '')).filter(Boolean).slice(0, 16);
while (SL.length < 16) SL.push('__none__');
if (out.length) out[0].json.slots = SL;
return out;
"""

FILTER = "=(&(objectClass=user)(|" + "".join("(sAMAccountName={{ $('Code: Match Pending').first().json.slots[%d] }})" % i for i in range(16)) + "))"

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const scope = ',' + String(cfg.scope_dn).toLowerCase();
const words = String(cfg.approve_words).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const maxAge = Number(cfg.pending_days) * 86400000;
const admins = new Set((($('[Redmine] Get IT Admins').first().json.users) || []).map(u => String(u.id)));
const one = v => Array.isArray(v) ? v[0] : v;
const ad = $input.all().map(i => i.json).filter(j => j.sAMAccountName);
const hist = {};
for (const { json: h } of $('[Redmine] Get Ticket History').all()) { const i = h.issue || h; if (i.id) hist[i.id] = i; }
const out = [];
for (const { json: t } of $('Code: Match Pending').all()) {
  const r = { ...t, action: 'escalate', reason: '' };
  const esc = reason => out.push({ json: { ...r, reason } });
  const issue = hist[t.ticket_id];
  if (!issue) { esc('не вдалося прочитати заявку'); continue; }
  const author = String(issue.author && issue.author.id);
  if (issue.status && issue.status.is_closed || ['17', '21'].includes(String(issue.status && issue.status.id))) {
    out.push({ json: { ...r, action: 'cancel', reason: 'задачу закрито без підтвердження' } }); continue;
  }
  // коментар-підтвердження після того, як агент підготував зміну
  const isWord = n => words.includes(String(n || '').trim().toLowerCase().replace(/[.!\s]+$/, ''));
  const notes = (issue.journals || []).filter(x => String(x.created_on) > String(t.prepared_at) && isWord(x.notes));
  const ok = notes.filter(x => admins.has(String(x.user && x.user.id)) && String(x.user.id) !== author);
  if (!ok.length) {
    if (notes.length) { esc(`підтвердження написав ${notes[0].user.name}, але він не з «IT Адміни» або є автором заявки`); continue; }
    if (Date.now() - Date.parse(t.prepared_at) > maxAge) { out.push({ json: { ...r, action: 'cancel', reason: `немає підтвердження понад ${cfg.pending_days} днів` } }); continue; }
    continue; // ще чекаємо
  }
  r.approved_by = ok[0].user.name;
  const hits = ad.filter(u => String(one(u.sAMAccountName)).toLowerCase() === t.plan.login);
  if (hits.length !== 1) { esc(`логін ${t.plan.login} не знайдено в AD`); continue; }
  const u = hits[0];
  if (String(u.dn) !== t.plan.dn) { esc('обліковий запис в AD змінився після підготовки (інший DN)'); continue; }
  if (!String(u.dn).toLowerCase().endsWith(scope)) { esc('обліковий запис поза дозволеним OU'); continue; }
  const pobox = String(one(u.postOfficeBox) || '').trim().toLowerCase();
  if (pobox !== (t.plan.old || '')) { esc('особиста пошта в AD змінилась після підготовки'); continue; }
  r.action = 'set_mail'; r.dn = u.dn; r.new_mail = t.plan.new;
  out.push({ json: r });
}
return out;
"""

REPORT_JS = r"""
const saluteOf = t => {  // як звертається автор заявки, якою мовою пише і як його звати
  const desc = String(t.description || '');
  const strip = x => String(x || '').replace(/https?:\/\/\S+|\S+@\S+|\b[\w-]+(?:[._][\w-]+)+\b/g, ' ');
  const txt = strip(desc).length >= 15 ? strip(desc) : strip(String(t.subject || '') + ' ' + desc);
  const cyr = (txt.match(/[\u0400-\u04FF]/g) || []).length, lat = (txt.match(/[A-Za-z]/g) || []).length;
  const lang = lat >= 8 && cyr * 10 < lat ? 'en' : 'uk';  // англійська, якщо кирилиці майже немає
  const s = (desc || String(t.subject || '')).toLowerCase().replace(/^[^\p{L}\p{N}]+/u, '').slice(0, 80);
  let hi;
  if (lang === 'en') hi = /^good\s+morning/.test(s) ? 'Good morning' : /^good\s+afternoon/.test(s) ? 'Good afternoon' : /^good\s+evening/.test(s) ? 'Good evening' : /^hi\b/.test(s) ? 'Hi' : 'Hello';
  else if (/^(доброго\s+ранку|добрий\s+ранок|доброе\s+утро)/.test(s)) hi = 'Доброго ранку';
  else if (/^(добрий\s+день|доброго\s+дня|добрый\s+день)/.test(s)) hi = 'Добрий день';
  else if (/^(добрий\s+вечір|доброго\s+вечора|добрый\s+вечер)/.test(s)) hi = 'Добрий вечір';
  else if (/^(вітаю|здрастуйте|здравствуйте)/.test(s)) hi = 'Вітаю';
  else hi = 'Привіт';
  const nm = String((t.author && t.author.name) || '').trim().split(/\s+/);
  const name = nm.length === 2 && /^[\p{L}'’-]+$/u.test(nm[1]) ? nm[1] : '';  // у Redmine ім'я у форматі «Прізвище Ім'я»; інші формати без імені
  return { lang, hi, name };
};
const _kh = Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Kyiv', hour: 'numeric', hourCycle: 'h23' }).format(new Date()));
// звертання, мова й ім'я автора: з Code: Parse Tickets (флоу-інструменти) або з тексту заявки (флоу Apply)
const _salOf = id => {
  try { const p = $('Code: Parse Tickets').all().find(x => String(x.json.ticket_id) === String(id)); if (p && p.json.salute) return p.json.salute; } catch (e) {}
  try { for (const { json: h } of $('[Redmine] Get Ticket History').all()) { const i = h.issue || h; if (String(i.id) === String(id)) return saluteOf(i); } } catch (e) {}
  return { lang: 'uk', hi: 'Привіт', name: '' };
};
// лише для публічних відповідей людям (укр. або англ. за мовою заявки); приватні нотатки для адміністраторів без привітань
const polite = (uk, en, id) => {
  const s = _salOf(id), nm = s.name ? ', ' + s.name : '';
  return s.lang === 'en'
    ? `${s.hi}${nm}! Thank you for your request.\n\n${en || uk}\n\n${_kh < 18 ? 'Have a nice day!' : 'Have a nice evening!'}`
    : `${s.hi}${nm}! Дякуємо за звернення.\n\n${uk}\n\n${_kh < 18 ? 'Гарного дня!' : 'Гарного вечора!'}`;
};
const cfg = $('Config').first().json;
return $input.all().map(({ json: j }) => {
  const p = j.plan || {};
  const failed = j.done === 'no';
  let note, noteEn = '', kind = 'set_mail', close = false, assign = false, details = j.reason || '';
  if (j.action === 'set_mail' && !failed) {
    close = true;
    details = `${p.old || '(порожньо)'} -> ${p.new}, підтвердив ${j.approved_by}`;
    note = `Особиста пошта для ${p.login} змінена, для генерації нового паролю скористайтеся порталом ${cfg.portal_url}`;
    noteEn = `The personal email for ${p.login} has been changed. To generate a new password, use the portal ${cfg.portal_url}`;
  } else if (j.action === 'cancel') {
    kind = 'cancelled';
    note = `Агент: зміну особистої пошти скасовано (${details}). Якщо вона ще потрібна, створіть нову заявку.`;
  } else {
    kind = 'escalate'; assign = true;
    if (failed) details = `помилка AD під час зміни пошти: ${j.error}`;
    note = `Агент: зміну пошти не виконано. Причина: ${details}. Потрібна перевірка людиною.`;
  }
  if (j.dry_run) {
    note = `DRY RUN (змін не внесено). Агент ${kind === 'set_mail' ? 'змінив би пошту ' + p.login + ' на ' + p.new + ' і закрив би задачу' : 'не виконав би зміну. Причина: ' + details}.`;
    close = false; assign = false;
  }
  return { json: { ticket_id: j.ticket_id, kind, note: (j.dry_run || kind !== 'set_mail') ? note : polite(note, noteEn, j.ticket_id), private: j.dry_run || kind !== 'set_mail', close, assign,
                   logins: p.login || '', outcome: kind === 'set_mail' && !j.dry_run ? 'applied' : kind, details,
                   payload: JSON.stringify(p), dry_run: j.dry_run, at: new Date().toISOString() } };
});
"""


def build(rm_id, rm_name, ldap_id, ldap_name, table_id):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    ldc = {"ldap": {"id": ldap_id, "name": ldap_name}}
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
    mark = lambda done: {"mode": "raw", "jsonOutput": "={{ JSON.stringify({ ...$('Code: Decide Apply').item.json, done: '%s', error: %s }) }}"
                         % (done, "String($json.error || 'невідома помилка')" if done == "no" else "''"), "options": {}}

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Apply Approved Changes\n"
        "Кожні 5 хв перевіряє заявки, для яких агент підготував зміну (аудит: awaiting_approval).\n"
        "Виконує лише після коментаря «Погоджую» від учасника «IT Адміни» (не автора заявки) і якщо в AD нічого не змінилось.\n"
        "Закрита задача або 7 днів без підтвердження = зміну скасовано.\n"
        "Зараз: зміна особистої пошти (postOfficeBox).\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює."), "height": 210, "width": 620}, -200, -300)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "approve_words", "value": "погоджую,підтверджую", "type": "string"},
        {"id": "a8", "name": "pending_days", "value": 7, "type": "number"},
        {"id": "a4", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a5", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a6", "name": "scope_dn", "value": "OU=AllUsers,OU=ORG,DC=example,DC=com", "type": "string"},
        {"id": "a7", "name": "portal_url", "value": PORTAL, "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Log", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("Code: Match Pending", "n8n-nodes-base.code", {"jsCode": MATCH_JS.strip()}, 880, 0, ver=2)
    node("[Redmine] Get Ticket History", RM, {"operation": "get", "issueId": "={{ $json.ticket_id }}", "include": ["journals"], "options": {}},
         1100, 0, credentials=rmc)
    node("[Redmine] Get IT Admins", RM, {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"group_id": cfgv("admins_group_id"), "status": "1"}, "options": {}}, 1320, 0, credentials=rmc,
         executeOnce=True, alwaysOutputData=True)
    node("[AD] Search Users", "n8n-nodes-base.ldap", {"baseDN": "DC=example,DC=com", "searchFor": "custom", "customFilter": FILTER,
         "returnAll": True, "options": {"scope": "sub", "attributes": ["sAMAccountName", "postOfficeBox"]}},
         1540, 0, credentials=ldc, executeOnce=True, alwaysOutputData=True)
    node("Code: Decide Apply", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1760, 0, ver=2)
    node("Apply Now?", "n8n-nodes-base.if", {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
         "conditions": [{"id": "c1", "leftValue": "={{ $json.action === 'set_mail' && !$json.dry_run }}", "rightValue": "",
                         "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}},
         1980, 0, ver=2.2)
    node("[AD] Update Personal Mail", "n8n-nodes-base.ldap", {"operation": "update", "dn": "={{ $json.dn }}",
         "attributes": {"replace": [{"id": "postOfficeBox", "value": "={{ $json.new_mail }}"}]}},
         2200, -160, credentials=ldc, onError="continueErrorOutput")
    node("Mark Done", "n8n-nodes-base.set", mark("yes"), 2420, -240, ver=3.4)
    node("Mark Failed", "n8n-nodes-base.set", mark("no"), 2420, -80, ver=3.4)
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 3}, 2640, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 2860, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 3080, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 3300, 0, ver=1.1)

    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Log"), ("[Data Table] Get Log", "Code: Match Pending"), ("Code: Match Pending", "[Redmine] Get Ticket History"),
                 ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins"), ("[Redmine] Get IT Admins", "[AD] Search Users"),
                 ("[AD] Search Users", "Code: Decide Apply"), ("Code: Decide Apply", "Apply Now?"),
                 ("Merge Results", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"),
                 ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Apply Now?", "[AD] Update Personal Mail", 0)
    link("Apply Now?", "Merge Results", 1, 2)
    link("[AD] Update Personal Mail", "Mark Done", 0)
    link("[AD] Update Personal Mail", "Mark Failed", 1)
    link("Mark Done", "Merge Results", 0, 0)
    link("Mark Failed", "Merge Results", 0, 1)
    return {"name": "Автоматизатор: Apply Approved Changes", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--redmine-cred", default="<REDMINE_CRED>")
    ap.add_argument("--redmine-name", default="Redmine <ENV> account")
    ap.add_argument("--ldap-cred", default="<LDAP_CRED>")
    ap.add_argument("--ldap-name", default="LDAP service account")
    ap.add_argument("--table-id", default="<AUDIT_TABLE>")
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-apply-approved.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.ldap_cred, a.ldap_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
