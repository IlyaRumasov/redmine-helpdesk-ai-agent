"""Збирає workflows/support-agent-google-guest.json (флоу «Support Agent: Google Guest Access»).

Заявки «не можу увійти в гостьовий акаунт Google, бо не налаштована 2FA»: агент генерує резервні коди
(Directory API verificationCodes) і надсилає кілька з них на робочу пошту ГА (<логін AD>@example.com).
Що видавати, вирішує стан гостя в Google (а не лише слова в заявці): лише код (без підтвердження), або тимчасовий пароль
(теж разом з кодами, якщо акаунт заблоковано 2FA) через підтвердження «Погоджую» (виконує флоу Google Guest Apply).
Коди не потрапляють у Redmine, аудит і (у бою) в історію запусків n8n: збереження даних запусків вимкнено.

Запуск: python tools/build_guest.py [--redmine-cred ID --redmine-name N --ldap-cred ID --ldap-name N --google-cred ID --google-name N
                                     --smtp-cred ID --smtp-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PARSE_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const allowed = String(cfg.allowed_author_ids).split(',').map(s => s.trim()).filter(Boolean);
const seen = new Set($('[Data Table] Get Processed').all()
  .filter(i => i.json.kind !== 'dispatch' && (dry || i.json.dry_run === false)).map(i => String(i.json.ticket_id)));
const work = '@' + String(cfg.work_mail_domain).toLowerCase();
const out = [];
for (const t of ($input.first().json.issues || [])) {
  // група «IT Адміни» (admins_group_id) = виконавець за замовчуванням, заявка ще нерозподілена
  if ((t.assigned_to && String(t.assigned_to.id) !== String(cfg.admins_group_id)) || seen.has(String(t.id))) continue;
  const text = `${t.subject}\n${t.description || ''}`.toLowerCase();
  // не просто «guest»: це слово трапляється в логінах (test.guest.N)
  const isGuest = /(гостьов|guest acc|guest\.google|google|гугл|gmail)/.test(text);
  const isAccess = /(2fa|2-step|двоетап|двохетап|двоетап|двофактор|двухфакт|подвійн|backup|резервн|код|sign-in settings|верифікац|не може увійти|втрат\S* доступ|відновлен\S* доступ)/.test(text);
  if (!(isGuest && isAccess) && !(t.ai && t.ai.type === 'guest_codes')) continue;
  const base = { ticket_id: t.id, login: null, reason: '' };
  const esc = reason => out.push({ ...base, kind: 'escalate', reason });
  if (!allowed.includes(String(t.author && t.author.id))) { esc('автор задачі не входить до списку координаторів'); continue; }
  // що просять: пароль, коди чи обидва (у заявках часто «скинути пароль та надіслати резервний код»)
  const wantPassword = /(забу\S* парол|forgot\S* password|не пам'ята\S* парол|скин\S* парол|зміна парол|змінити парол|новий парол|reset\S* password|парол)/.test(text);
  const wantCodes = /(резервн|backup|код|code|двоетап|двохетап|двофактор|двухфакт|2fa|2-step|sign-in settings)/.test(text);
  // логін AD: з гостьової адреси, з робочої пошти або з тексту «логін АД: x»
  const logins = new Set();
  for (const m of text.matchAll(/([a-z0-9][a-z0-9._-]*)_example\.com@[\w.-]+\.guest\.google/g)) logins.add(m[1]);
  for (const m of text.matchAll(/([a-z0-9][a-z0-9._-]*)@([\w.-]+)/g)) if ('@' + m[2] === work) logins.add(m[1]);
  for (const m of text.matchAll(/лог[іi]н\s*(?:ад|ad)?\s*[:\-–]\s*([a-z0-9][a-z0-9._-]*[a-z0-9])/g)) logins.add(m[1]);
  let L = [...logins].filter(x => /[._]/.test(x));
  if (!L.length && t.ai) L = [...new Set((t.ai.logins || []).map(x => String(x).toLowerCase()).filter(x => /^[a-z0-9][a-z0-9._-]*[a-z0-9]$/.test(x) && /[._]/.test(x)))];
  if (L.length !== 1) { esc(L.length ? `у заявці кілька облікових записів (${L.join(', ')})` : 'не знайшов у заявці логіну AD або робочої/гостьової адреси'); continue; }
  base.login = L[0];
  base.guest = `${L[0]}_example.com@${cfg.guest_domain}.guest.google`;
  base.work_mail = L[0] + work;
  out.push({ ...base, kind: 'access', want_password: wantPassword, want_codes: wantCodes || !wantPassword });
}
// Слоти для пошуку в AD (нода LDAP не приймає фільтр цілком із виразу): усі логіни, до 16, зайві = __none__
const SL = [...new Set(out.map(x => x.login).filter(Boolean))].map(x => String(x).replace(/[*()\\\u0000]/g, '')).filter(Boolean).slice(0, 16);
while (SL.length < 16) SL.push('__none__');
if (out.length) out[0].slots = SL;
return out.map(json => ({ json }));
"""

FILTER = "=(&(objectClass=user)(|" + "".join("(sAMAccountName={{ $('Code: Parse Tickets').first().json.slots[%d] }})" % i for i in range(16)) + "))"

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const scope = ',' + String(cfg.scope_dn).toLowerCase();
const one = v => Array.isArray(v) ? v[0] : v;
const ad = $input.all().map(i => i.json).filter(j => j.sAMAccountName);
const tickets = $('Code: Parse Tickets').all();
const google = $('[Google] Get Guest').all();
return tickets.map(({ json: t }, i) => {
  const r = { ...t, dry_run: dry, action: 'escalate' };
  const esc = reason => ({ json: { ...r, reason } });
  if (t.kind === 'escalate') return { json: r };
  const hits = ad.filter(u => String(one(u.sAMAccountName)).toLowerCase() === t.login);
  if (hits.length !== 1) return esc(`логін ${t.login} не знайдено в AD`);
  const u = hits[0];
  if (!String(u.dn).toLowerCase().endsWith(scope)) return esc('обліковий запис AD поза дозволеним OU (можливо, звільнений)');
  if ((parseInt(one(u.userAccountControl), 10) || 0) & 2) return esc('обліковий запис AD вимкнено');
  const g = google[i] && google[i].json || {};
  const b = g.body || {};
  if (g.statusCode !== 200 || !b.primaryEmail) return esc(`гостьовий акаунт ${t.guest} не знайдено в Google (код ${g.statusCode})`);
  if (!b.isGuestUser) return esc('це не гостьовий акаунт Google');
  if (b.isAdmin || b.isDelegatedAdmin) return esc('акаунт Google має права адміністратора');
  if (b.suspended) return esc('гостьовий акаунт Google призупинено');
  const owner = String((b.guestAccountInfo || {}).primaryGuestEmail || '').toLowerCase();
  if (owner !== t.work_mail) return esc(`гостьовий акаунт прив'язано до іншої адреси (${owner || 'невідомо'})`);
  // стан 2FA: льготний період Google = 7 днів від першого входу; після дати примусового впровадження блокуються всі без 2FA
  const enrolled = !!b.isEnrolledIn2Sv;
  const last = String(b.lastLoginTime || '');
  const never = !last || last.startsWith('1970');
  const days = never ? null : (Date.now() - Date.parse(last)) / 86400000;
  const pastEnforce = Date.now() >= Date.parse(String(cfg.enforce_date));
  const blocked = !enrolled && !never && (pastEnforce || days >= 7);
  Object.assign(r, { enrolled_2sv: enrolled, never_logged_in: never, blocked });
  if (enrolled && !t.want_password) return esc('у гостя вже налаштована 2FA, резервні коди не потрібні (ймовірно, потрібен новий пароль)');
  if (never && !t.want_password) return esc('гість ще ні разу не входив: резервні коди не потрібні, спершу має зареєструватись за запрошенням');
  if (t.want_password) {
    // пароль — це повний вхід в акаунт: лише через підтвердження; якщо акаунт заблоковано 2FA, додаємо коди
    r.action = 'request_approval';
    r.set_password = true;
    r.send_codes = !enrolled && (blocked || t.want_codes);
    return { json: r };
  }
  r.action = 'codes';
  return { json: r };
});
"""

MAIL_JS = r"""
// Збирає лист з N кодами. Коди лишаються лише в цьому вузлі й у листі.
const cfg = $('Config').first().json;
const d = $('Code: Decide Guest Action').item.json;
const items = (($json.body || {}).items || []).map(x => x.verificationCode).filter(Boolean);
const n = Number(cfg.codes_in_mail);
if (items.length < n) return { json: { ok: false, error: `Google повернув ${items.length} кодів` } };
const codes = items.slice(0, n);
const text = [
  'Hello! / Вітаємо!',
  '',
  'Here are backup codes to sign in to your Google guest account (Google Chat).',
  'Нижче резервні коди для входу в гостьовий акаунт Google (Google Chat).',
  '',
  `Account / Акаунт: ${d.guest}`,
  'Sign in with this full address and your password. When Google asks for 2-Step Verification, choose "Try another way" -> "Enter a backup code".',
  'Увійдіть під цією повною адресою та своїм паролем. Коли Google попросить другий крок, оберіть «Інший спосіб» -> «Резервний код».',
  '',
  ...codes.map((c, k) => `${k + 1}. ${c}`),
  '',
  'Each code works once. Right after signing in, set up 2-Step Verification, otherwise you will be locked out again.',
  'Кожен код діє один раз. Одразу після входу налаштуйте двоетапну перевірку, інакше доступ знову заблокується.',
  '',
  'IT support / IT підтримка',
].join('\n');
return { json: { ok: true, to: d.work_mail, subject: 'Google guest account: backup codes / резервні коди', text } };
"""

REPORT_JS = r"""
return $input.all().map(({ json: j }) => {
  let note, kind = j.action, close = false, assign = false, details = j.reason || '';
  const failed = j.done === 'no';
  let approval = false, payload = '';
  if (j.action === 'request_approval') {
    kind = 'guest_access'; assign = true; approval = true;
    payload = JSON.stringify({ login: j.login, guest: j.guest, work_mail: j.work_mail, set_password: true, send_codes: !!j.send_codes });
    details = j.send_codes ? 'тимчасовий пароль + резервні коди' : 'тимчасовий пароль';
    note = `Агент підготував видачу доступу до гостьового акаунту ${j.guest}: ${details} (листи підуть на ${j.work_mail}).`
      + (j.blocked ? ' Акаунт заблоковано через відсутність 2FA, тому потрібні й пароль, і коди.' : '')
      + ' Щоб виконати, учасник «IT Адміни» пише в задачі коментар «Погоджую». Через 7 днів без підтвердження дію скасовано.';
  } else if (j.action === 'codes' && !failed) {
    close = true;
    details = `коди надіслано на ${j.work_mail}`;
    note = `Вітаю. Лист із резервними кодами для входу в гостьовий акаунт Google надіслано на робочу пошту ${j.work_mail}. Після входу ГА потрібно одразу налаштувати двоетапну перевірку.`;
  } else {
    kind = 'escalate'; assign = true;
    if (failed) details = `помилка під час видачі кодів: ${j.error}`;
    note = `Агент: автоматично не обробляю. Причина: ${details}. Передано адміністраторам.`;
  }
  if (j.dry_run) {
    note = `DRY RUN (змін не внесено). Агент ${j.action === 'codes' ? 'згенерував би резервні коди для ' + j.guest + ' і надіслав би на ' + j.work_mail
      : j.action === 'request_approval' ? 'підготував би видачу доступу до ' + j.guest + ' (' + details + ') і чекав би «Погоджую»' : 'передав би задачу адміністраторам. Причина: ' + details}.`;
    close = false; assign = false;  // approval лишається: сухий рядок awaiting_approval дозволяє флоу Apply показати «виконав би»
  }
  return { json: { ticket_id: j.ticket_id, kind, note, private: j.dry_run || kind !== 'codes', close, assign,
                   logins: j.login || '', outcome: approval ? 'awaiting_approval' : kind, details, payload, dry_run: j.dry_run,
                   at: new Date().toISOString() } };
});
"""


def build(rm_id, rm_name, ldap_id, ldap_name, g_id, g_name, smtp_id, smtp_name, table_id):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    ldc = {"ldap": {"id": ldap_id, "name": ldap_name}}
    gc = {"googleApi": {"id": g_id, "name": g_name}}
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
    API = "https://admin.googleapis.com/admin/directory/v1/users/"
    cfgv = lambda k: "={{ $('Config').first().json.%s }}" % k
    table = {"__rl": True, "mode": "id", "value": table_id}
    gapi = lambda method, url: {"method": method, "url": url, "authentication": "predefinedCredentialType", "nodeCredentialType": "googleApi",
                                "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}
    if_true = lambda name, expr, x, y: node(name, "n8n-nodes-base.if", {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c", "leftValue": expr, "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}}, x, y, ver=2.2)
    mark = lambda done: {"mode": "raw", "jsonOutput": "={{ JSON.stringify({ ...$('Code: Decide Guest Action').item.json, done: '%s', error: %s }) }}"
                         % (done, "String($json.error && ($json.error.message || $json.error) || 'невідома помилка')" if done == "no" else "''"), "options": {}}

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Support Agent: Google Guest Access\n"
        "Кожні 5 хв бере нові нерозподілені задачі HelpDesk «не можу увійти в гостьовий акаунт Google (2FA)».\n"
        "Перевіряє координатора, AD (працює, увімкнений, у дозволеному OU) і гостя Google (існує, гостьовий, прив'язаний до робочої пошти),\n"
        "дивиться стан гостя: лише код -> видає й надсилає на <логін>@example.com; пароль (з кодами, якщо акаунт заблоковано) -> готує дію\n"
        "і чекає коментаря «Погоджую» (виконує флоу Google Guest Apply). Нестандартне передає «IT Адміни».\n"
        "Збереження даних запусків вимкнено: коди не лишаються в n8n.\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює й не надсилає."), "height": 230, "width": 660}, -200, -320)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "tracker_id", "value": "<TRACKER_ID>", "type": "string"},
        {"id": "a4", "name": "allowed_author_ids", "value": "<COORDINATOR_IDS>", "type": "string"},
        {"id": "a5", "name": "scope_dn", "value": "OU=AllUsers,OU=ORG,DC=example,DC=com", "type": "string"},
        {"id": "a6", "name": "guest_domain", "value": "guests.example.com", "type": "string"},
        {"id": "a7", "name": "work_mail_domain", "value": "example.com", "type": "string"},
        {"id": "a8", "name": "codes_in_mail", "value": 3, "type": "number"},
        {"id": "a9", "name": "mail_from", "value": "<MAIL_FROM>", "type": "string"},
        {"id": "a10", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a11", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a12", "name": "enforce_date", "value": "2026-10-30", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Processed", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get New Tickets", RM, {"operation": "getAll", "returnAll": True, "filters": {
         "project_id": cfgv("project_id"), "tracker_id": cfgv("tracker_id"), "status_id": "1"}, "options": {}}, 660, 0, credentials=rmc)
    node("Code: Parse Tickets", "n8n-nodes-base.code", {"jsCode": PARSE_JS.strip()}, 880, 0, ver=2)
    node("[AD] Search Users", "n8n-nodes-base.ldap", {"baseDN": "DC=example,DC=com", "searchFor": "custom", "customFilter": FILTER,
         "returnAll": True, "options": {"scope": "sub", "attributes": ["sAMAccountName", "userAccountControl"]}},
         1320, 0, credentials=ldc, executeOnce=True, alwaysOutputData=True)
    node("[Google] Get Guest", "n8n-nodes-base.httpRequest",
         gapi("GET", "={{ $json.guest ? '" + API + "' + encodeURIComponent($json.guest) + '?projection=basic' : '"
              + API + "none%40invalid' }}"), 1100, 0, ver=4.2, credentials=gc, onError="continueRegularOutput")
    node("Code: Decide Guest Action", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1540, 0, ver=2)
    if_true("Issue Codes Now?", "={{ $json.action === 'codes' && !$json.dry_run }}", 1760, 0)  # решта (підготовка до підтвердження, ескалація) йде прямо у звіт
    node("[Google] Generate Codes", "n8n-nodes-base.httpRequest",
         {**gapi("POST", "=" + API + "{{ encodeURIComponent($json.guest) }}/verificationCodes/generate"),
          "options": {}}, 1980, -200, ver=4.2, credentials=gc, onError="continueErrorOutput")
    node("[Google] List Codes", "n8n-nodes-base.httpRequest",
         gapi("GET", "=" + API + "{{ encodeURIComponent($('Code: Decide Guest Action').item.json.guest) }}/verificationCodes"),
         2200, -200, ver=4.2, credentials=gc, onError="continueErrorOutput")
    node("Code: Build Mail", "n8n-nodes-base.code", {"mode": "runOnceForEachItem", "jsCode": MAIL_JS.strip()}, 2420, -200, ver=2)
    if_true("Mail Ready?", "={{ $json.ok === true }}", 2640, -200)
    node("[Mail] Send Codes", "n8n-nodes-base.emailSend", {"fromEmail": cfgv("mail_from"), "toEmail": "={{ $json.to }}",
         "subject": "={{ $json.subject }}", "emailFormat": "text", "text": "={{ $json.text }}", "options": {"appendAttribution": False}},
         2860, -300, ver=2.1, credentials={"smtp": {"id": smtp_id, "name": smtp_name}}, onError="continueErrorOutput")
    node("Mark Done", "n8n-nodes-base.set", mark("yes"), 3080, -340, ver=3.4)
    node("Mark Failed", "n8n-nodes-base.set", mark("no"), 3080, -160, ver=3.4)
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 3}, 3300, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 3520, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 3740, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 3960, 0, ver=1.1)

    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Processed"), ("[Data Table] Get Processed", "[Redmine] Get New Tickets"),
                 ("[Redmine] Get New Tickets", "Code: Parse Tickets"), ("Code: Parse Tickets", "[Google] Get Guest"),
                 ("[Google] Get Guest", "[AD] Search Users"), ("[AD] Search Users", "Code: Decide Guest Action"),
                 ("Code: Decide Guest Action", "Issue Codes Now?"), ("[Google] Generate Codes", "[Google] List Codes"),
                 ("[Google] List Codes", "Code: Build Mail"), ("Code: Build Mail", "Mail Ready?"), ("[Mail] Send Codes", "Mark Done"),
                 ("Merge Results", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"),
                 ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Issue Codes Now?", "[Google] Generate Codes", 0)
    link("Issue Codes Now?", "Merge Results", 1, 2)
    link("[Google] Generate Codes", "Mark Failed", 1)
    link("[Google] List Codes", "Mark Failed", 1)
    link("Mail Ready?", "[Mail] Send Codes", 0)
    link("Mail Ready?", "Mark Failed", 1)
    link("[Mail] Send Codes", "Mark Failed", 1)
    link("Mark Done", "Merge Results", 0, 0)
    link("Mark Failed", "Merge Results", 0, 1)
    settings = {"executionOrder": "v1", "saveDataSuccessExecution": "none", "saveDataErrorExecution": "none",
                "saveManualExecutions": False, "saveExecutionProgress": False}
    return {"name": "Support Agent: Google Guest Access", "nodes": nodes, "connections": conn, "settings": settings}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("ldap-cred", "<LDAP_CRED>"),
                 ("ldap-name", "LDAP service account"), ("google-cred", "<GOOGLE_SA_CRED>"), ("google-name", "Google service account"),
                 ("smtp-cred", "<SMTP_CRED>"), ("smtp-name", "IT mailbox SMTP"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-google-guest.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.ldap_cred, a.ldap_name, a.google_cred, a.google_name, a.smtp_cred, a.smtp_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
