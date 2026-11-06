"""Збирає workflows/support-agent-google-guest-apply.json (флоу «Автоматизатор: Google Guest Apply»).

Виконує видачу доступу до гостьового акаунта Google, яку підготував флоу Google Guest Access (аудит: kind guest_access,
outcome awaiting_approval), після коментаря «Погоджую» від учасника «IT Адміни» (не автора заявки):
тимчасовий пароль (зі зміною при першому вході) і, якщо потрібно, резервні коди; два окремі листи на робочу пошту <логін>@example.com.
Пароль генерується тут, у момент виконання, і ніде не зберігається (ні в аудиті, ні в Redmine); збереження даних запусків вимкнено.

Запуск: python tools/build_guest_apply.py [--redmine-cred ID --redmine-name N --google-cred ID --google-name N --smtp-cred ID --smtp-name N --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
  if (r.kind !== 'guest_access' || r.outcome !== 'awaiting_approval') continue;
  let p;
  try { p = JSON.parse(r.payload); } catch (e) { continue; }
  out.push({ json: { ticket_id: r.ticket_id, plan: p, prepared_at: r.at, dry_run: dry } });
}
return out;
"""

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const words = String(cfg.approve_words).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const maxAge = Number(cfg.pending_days) * 86400000;
const admins = new Set((($('[Redmine] Get IT Admins').first().json.users) || []).map(u => String(u.id)));
const pend = $('Code: Match Pending').all();
const google = $('[Google] Get Guest').all();
const hist = $('[Redmine] Get Ticket History').all();
const out = [];
pend.forEach(({ json: t }, i) => {
  const r = { ...t, action: 'escalate', reason: '' };
  const esc = reason => out.push({ json: { ...r, reason } });
  const h = hist[i] && hist[i].json || {};
  const issue = h.issue || h;
  if (!issue.id) { esc('не вдалося прочитати заявку'); return; }
  const author = String(issue.author && issue.author.id);
  if ((issue.status && issue.status.is_closed) || ['17', '21'].includes(String(issue.status && issue.status.id))) {
    out.push({ json: { ...r, action: 'cancel', reason: 'задачу закрито без підтвердження' } }); return;
  }
  const isWord = n => words.includes(String(n || '').trim().toLowerCase().replace(/[.!\s]+$/, ''));
  const notes = (issue.journals || []).filter(x => String(x.created_on) > String(t.prepared_at) && isWord(x.notes));
  const ok = notes.filter(x => admins.has(String(x.user && x.user.id)) && String(x.user.id) !== author);
  if (!ok.length) {
    if (notes.length) { esc(`підтвердження написав ${notes[0].user.name}, але він не з «IT Адміни» або є автором заявки`); return; }
    if (Date.now() - Date.parse(t.prepared_at) > maxAge) { out.push({ json: { ...r, action: 'cancel', reason: `немає підтвердження понад ${cfg.pending_days} днів` } }); return; }
    return;  // ще чекаємо
  }
  r.approved_by = ok[0].user.name;
  // ще раз звіряємо гостя: той самий акаунт, прив'язаний до цієї робочої пошти, без прав адміністратора
  const g = google[i] && google[i].json || {};
  const b = g.body || {};
  if (g.statusCode !== 200 || !b.primaryEmail) { esc(`гостьовий акаунт ${t.plan.guest} не знайдено в Google (код ${g.statusCode})`); return; }
  if (!b.isGuestUser || b.isAdmin || b.isDelegatedAdmin) { esc('акаунт Google не гостьовий або має права адміністратора'); return; }
  if (b.suspended) { esc('гостьовий акаунт Google призупинено'); return; }
  const owner = String((b.guestAccountInfo || {}).primaryGuestEmail || '').toLowerCase();
  if (owner !== String(t.plan.work_mail).toLowerCase()) { esc(`гостьовий акаунт прив'язано до іншої адреси (${owner || 'невідомо'})`); return; }
  out.push({ json: { ...r, action: 'apply' } });
});
return out;
"""

SECRETS_JS = r"""
// Тимчасовий пароль зі справжньої випадковості вузла Crypto (у Code-вузлі крипто-модуль заборонено).
// Алфавіт без символів, що плутаються; гарантовано є велика, мала літера й цифра. Не зберігається ніде, окрім листа.
const UP = 'ABCDEFGHJKLMNPQRSTUVWXYZ', LO = 'abcdefghijkmnpqrstuvwxyz', DG = '23456789', ALL = UP + LO + DG;
const r = String($json.rnd || '').replace(/[^A-Za-z0-9]/g, '');
if (r.length < 20) throw new Error('генератор випадкових чисел не повернув достатньо даних');
const code = i => r.charCodeAt(i);
const parts = [UP[code(0) % UP.length], LO[code(1) % LO.length], DG[code(2) % DG.length]];
for (let i = 3; i < 16; i++) parts.push(ALL[(code(i) * 7 + code(i + 1)) % ALL.length]);
// порядок символів теж із випадкових даних (проста тасовка Фішера-Єйтса)
for (let i = parts.length - 1; i > 0; i--) { const j = code(i + 3) % (i + 1); [parts[i], parts[j]] = [parts[j], parts[i]]; }
const { rnd, ...rest } = $json;
return { json: { ...rest, password: parts.join('') } };
"""

MAILS_JS = r"""
// Два окремі листи: пароль і коди. Секрети лишаються лише в цьому вузлі й у листах.
const cfg = $('Config').first().json;
const d = $('Code: Generate Secrets').item.json;
const plan = d.plan;
let codes = [];
try { codes = (($('[Google] List Codes').item.json.body || {}).items || []).map(x => x.verificationCode).filter(Boolean).slice(0, Number(cfg.codes_in_mail)); } catch (e) { codes = []; }
if (plan.send_codes && codes.length < Number(cfg.codes_in_mail)) return { json: { ok: false, error: `Google повернув ${codes.length} кодів` } };
const pwText = [
  'Hello! / Вітаємо!',
  '',
  `Temporary password for your Google guest account / Тимчасовий пароль до гостьового акаунту Google: ${plan.guest}`,
  '',
  `Password / Пароль: ${d.password}`,
  '',
  'Sign in with the full address above. Google will ask you to set a new password (at least 12 characters).',
  'Увійдіть під повною адресою вище. Google попросить задати новий пароль (не менше 12 символів).',
  '',
  plan.send_codes ? 'Backup codes will arrive in a separate email. / Резервні коди прийдуть окремим листом.' : '',
  'After signing in, set up 2-Step Verification right away. / Після входу одразу налаштуйте двоетапну перевірку.',
  '',
  'IT support / IT підтримка',
].filter((x, i, a) => !(x === '' && a[i - 1] === '')).join('\n');
const codeText = [
  'Hello! / Вітаємо!',
  '',
  `Backup codes for your Google guest account / Резервні коди для гостьового акаунту Google: ${plan.guest}`,
  '',
  'When Google asks for 2-Step Verification, choose "Try another way" -> "Enter one of your 8-digit backup codes".',
  'Коли Google попросить другий крок, оберіть «Інший спосіб» -> «Резервний код».',
  '',
  ...codes.map((c, k) => `${k + 1}. ${c}`),
  '',
  'Each code works once. Set up 2-Step Verification right after signing in. / Кожен код діє один раз. Одразу після входу налаштуйте двоетапну перевірку.',
  '',
  'IT support / IT підтримка',
].join('\n');
return { json: { ok: true, to: plan.work_mail, has_codes: !!plan.send_codes, pw_subject: 'Google guest account: temporary password / тимчасовий пароль',
                 pw_text: pwText, codes_subject: 'Google guest account: backup codes / резервні коди', codes_text: codeText } };
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
  let note, noteEn = '', outcome, close = false, assign = false, details = j.reason || '';
  if (j.action === 'apply' && !failed) {
    outcome = 'applied'; close = true;
    details = p.send_codes ? 'тимчасовий пароль і резервні коди надіслано' : 'тимчасовий пароль надіслано';
    note = `${p.send_codes ? 'Тимчасовий пароль і резервні коди' : 'Тимчасовий пароль'} для гостьового акаунту ${p.guest} надіслано на робочу пошту ${p.work_mail}`
      + `${p.send_codes ? ' окремими листами' : ''}. Увійдіть під повною адресою гостьового акаунту, задайте новий пароль${p.send_codes ? ', на другому кроці оберіть «Інший спосіб» -> «Резервний код»' : ''} і одразу налаштуйте двоетапну перевірку.`;
    noteEn = `${p.send_codes ? 'A temporary password and backup codes' : 'A temporary password'} for the guest account ${p.guest} ${p.send_codes ? 'have' : 'has'} been sent to your work address ${p.work_mail}${p.send_codes ? ' in separate emails' : ''}. Sign in with the full guest account address, set a new password${p.send_codes ? ', and at the second step choose "Try another way" -> "Enter a backup code"' : ''}, then set up 2-Step Verification right away.`;
  } else if (j.action === 'cancel') {
    outcome = 'cancelled';
    note = `Агент: видачу доступу до гостьового акаунту скасовано (${details}). Якщо вона ще потрібна, створіть нову заявку.`;
  } else {
    outcome = 'escalate'; assign = true;
    if (failed) details = `помилка під час видачі: ${j.error}. Увага: тимчасовий пароль міг бути вже встановлений, але лист не дійшов; потрібно видати доступ вручну`;
    note = `Агент: видачу доступу не виконано. Причина: ${details}. Потрібна перевірка людиною.`;
  }
  if (j.dry_run) {
    note = `DRY RUN (змін не внесено). Агент ${j.action === 'apply' ? 'видав би доступ до ' + p.guest + ' (' + (p.send_codes ? 'пароль + коди' : 'пароль') + ') на ' + p.work_mail : 'не виконав би видачу. Причина: ' + details}.`;
    close = false; assign = false;
  }
  return { json: { ticket_id: j.ticket_id, kind: 'guest_access', note: (j.dry_run || outcome !== 'applied') ? note : polite(note, noteEn, j.ticket_id), private: j.dry_run || outcome !== 'applied', close, assign,
                   logins: p.login || '', outcome: j.dry_run ? 'dry_' + outcome : outcome, details, payload: JSON.stringify(p), dry_run: j.dry_run, at: new Date().toISOString() } };
});
"""


def build(rm_id, rm_name, g_id, g_name, smtp_id, smtp_name, table_id):
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    gc = {"googleApi": {"id": g_id, "name": g_name}}
    smtp = {"smtp": {"id": smtp_id, "name": smtp_name}}
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

    API = "https://admin.googleapis.com/admin/directory/v1/users/"
    table = {"__rl": True, "mode": "id", "value": table_id}
    gapi = lambda method, url: {"method": method, "url": url, "authentication": "predefinedCredentialType", "nodeCredentialType": "googleApi",
                                "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}
    if_true = lambda name, expr, x, y: node(name, "n8n-nodes-base.if", {"conditions": {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": "c", "leftValue": expr, "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
        "combinator": "and"}, "options": {}}, x, y, ver=2.2)
    mark = lambda done: {"mode": "raw", "jsonOutput": "={{ JSON.stringify({ ...$('Code: Decide Apply').item.json, done: '%s', error: %s }) }}"
                         % (done, "String($json.error && ($json.error.message || $json.error) || 'невідома помилка')" if done == "no" else "''"), "options": {}}
    mail = lambda name, subj, text, x, y: node(name, "n8n-nodes-base.emailSend", {
        "fromEmail": "={{ $('Config').first().json.mail_from }}", "toEmail": "={{ $('Code: Build Mails').item.json.to }}",
        "subject": "={{ $('Code: Build Mails').item.json.%s }}" % subj, "emailFormat": "text",
        "text": "={{ $('Code: Build Mails').item.json.%s }}" % text, "options": {"appendAttribution": False}},
        x, y, ver=2.1, credentials=smtp, onError="continueErrorOutput",
        retryOnFail=True, maxTries=4, waitBetweenTries=20000)  # SMTP-релей Google інколи відповідає 421 «Try again later»

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Google Guest Apply\n"
        "Кожні 5 хв перевіряє заявки, для яких флоу Google Guest Access підготував видачу доступу (аудит: guest_access / awaiting_approval).\n"
        "Після коментаря «Погоджую» від учасника «IT Адміни» (не автора заявки), за умови що гість у Google той самий:\n"
        "ставить тимчасовий пароль (зі зміною при вході) і, якщо акаунт заблоковано без 2FA, генерує резервні коди; два листи на <логін>@example.com.\n"
        "Закрита задача або 7 днів без підтвердження = скасовано. Збереження даних запусків вимкнено: пароль і коди не лишаються в n8n.\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює й не надсилає."), "height": 250, "width": 700}, -200, -340)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "approve_words", "value": "погоджую,підтверджую", "type": "string"},
        {"id": "a3", "name": "pending_days", "value": 7, "type": "number"},
        {"id": "a4", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a5", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a6", "name": "codes_in_mail", "value": 3, "type": "number"},
        {"id": "a7", "name": "mail_from", "value": "<MAIL_FROM>", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Log", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("Code: Match Pending", "n8n-nodes-base.code", {"jsCode": MATCH_JS.strip()}, 660, 0, ver=2)
    node("[Google] Get Guest", "n8n-nodes-base.httpRequest", gapi("GET", "=" + API + "{{ encodeURIComponent($json.plan.guest) }}?projection=basic"),
         880, 0, ver=4.2, credentials=gc, onError="continueRegularOutput")
    node("[Redmine] Get Ticket History", "n8n-nodes-redmine.redmine", {"operation": "get",
         "issueId": "={{ $('Code: Match Pending').item.json.ticket_id }}", "include": ["journals"], "options": {}}, 1100, 0, credentials=rmc)
    node("[Redmine] Get IT Admins", "n8n-nodes-redmine.redmine", {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"group_id": "={{ $('Config').first().json.admins_group_id }}", "status": "1"}, "options": {}}, 1320, 0,
         credentials=rmc, executeOnce=True, alwaysOutputData=True)
    node("Code: Decide Apply", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1540, 0, ver=2)
    if_true("Apply Now?", "={{ $json.action === 'apply' && !$json.dry_run }}", 1760, 0)
    node("[Crypto] Random Bytes", "n8n-nodes-base.crypto", {"action": "generate", "dataPropertyName": "rnd", "encodingType": "base64", "stringLength": 32},
         1980, -200, ver=1)
    node("Code: Generate Secrets", "n8n-nodes-base.code", {"mode": "runOnceForEachItem", "jsCode": SECRETS_JS.strip()}, 2090, -200, ver=2,
         onError="continueErrorOutput")
    node("[Google] Set Temp Password", "n8n-nodes-base.httpRequest",
         {**gapi("PUT", "=" + API + "{{ encodeURIComponent($json.plan.guest) }}"), "sendBody": True, "specifyBody": "json",
          "jsonBody": "={{ JSON.stringify({ password: $json.password, changePasswordAtNextLogin: true }) }}"},
         2200, -200, ver=4.2, credentials=gc, onError="continueErrorOutput")
    if_true("Need Codes?", "={{ $('Code: Generate Secrets').item.json.plan.send_codes === true }}", 2420, -200)
    node("[Google] Generate Codes", "n8n-nodes-base.httpRequest",
         {**gapi("POST", "=" + API + "{{ encodeURIComponent($('Code: Generate Secrets').item.json.plan.guest) }}/verificationCodes/generate"), "options": {}},
         2640, -320, ver=4.2, credentials=gc, onError="continueErrorOutput")
    node("Wait: Codes Ready", "n8n-nodes-base.wait", {"amount": 10, "unit": "seconds"}, 2750, -320, ver=1.1)  # Google віддає список кодів із затримкою: без паузи список порожній
    node("[Google] List Codes", "n8n-nodes-base.httpRequest",
         gapi("GET", "=" + API + "{{ encodeURIComponent($('Code: Generate Secrets').item.json.plan.guest) }}/verificationCodes"),
         2860, -320, ver=4.2, credentials=gc, onError="continueErrorOutput")
    node("Code: Build Mails", "n8n-nodes-base.code", {"mode": "runOnceForEachItem", "jsCode": MAILS_JS.strip()}, 3080, -200, ver=2)
    if_true("Mails Ready?", "={{ $json.ok === true }}", 3300, -200)
    mail("[Mail] Send Password", "pw_subject", "pw_text", 3520, -300)
    if_true("Send Codes Mail?", "={{ $('Code: Build Mails').item.json.has_codes === true }}", 3740, -300)
    mail("[Mail] Send Codes", "codes_subject", "codes_text", 3960, -400)
    node("Mark Done", "n8n-nodes-base.set", mark("yes"), 4180, -360, ver=3.4)
    node("Mark Failed", "n8n-nodes-base.set", mark("no"), 4180, -120, ver=3.4)
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 3}, 4400, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 4620, 0, ver=2)
    node("[Redmine] Update Ticket", "n8n-nodes-redmine.redmine", {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 4840, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"),
            ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 5060, 0, ver=1.1)

    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Log"), ("[Data Table] Get Log", "Code: Match Pending"),
                 ("Code: Match Pending", "[Google] Get Guest"), ("[Google] Get Guest", "[Redmine] Get Ticket History"),
                 ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins"), ("[Redmine] Get IT Admins", "Code: Decide Apply"),
                 ("Code: Decide Apply", "Apply Now?"), ("Code: Generate Secrets", "[Google] Set Temp Password"),
                 ("[Google] Generate Codes", "Wait: Codes Ready"), ("Wait: Codes Ready", "[Google] List Codes"), ("[Google] List Codes", "Code: Build Mails"),
                 ("Code: Build Mails", "Mails Ready?"), ("Mark Done", "Merge Results"), ("Merge Results", "Code: Build Report"),
                 ("Code: Build Report", "[Redmine] Update Ticket"), ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Apply Now?", "[Crypto] Random Bytes", 0)
    link("[Crypto] Random Bytes", "Code: Generate Secrets")
    link("Code: Generate Secrets", "Mark Failed", 1)
    link("Apply Now?", "Merge Results", 1, 2)
    link("[Google] Set Temp Password", "Need Codes?", 0)
    link("[Google] Set Temp Password", "Mark Failed", 1)
    link("Need Codes?", "[Google] Generate Codes", 0)
    link("Need Codes?", "Code: Build Mails", 1)
    link("[Google] Generate Codes", "Mark Failed", 1)
    link("[Google] List Codes", "Mark Failed", 1)
    link("Mails Ready?", "[Mail] Send Password", 0)
    link("Mails Ready?", "Mark Failed", 1)
    link("[Mail] Send Password", "Send Codes Mail?", 0)
    link("[Mail] Send Password", "Mark Failed", 1)
    link("Send Codes Mail?", "[Mail] Send Codes", 0)
    link("Send Codes Mail?", "Mark Done", 1)
    link("[Mail] Send Codes", "Mark Done", 0)
    link("[Mail] Send Codes", "Mark Failed", 1)
    link("Mark Failed", "Merge Results", 0, 1)
    settings = {"executionOrder": "v1", "saveDataSuccessExecution": "none", "saveDataErrorExecution": "none",
                "saveManualExecutions": False, "saveExecutionProgress": False}
    return {"name": "Автоматизатор: Google Guest Apply", "nodes": nodes, "connections": conn, "settings": settings}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("google-cred", "<GOOGLE_SA_CRED>"),
                 ("google-name", "Google service account"), ("smtp-cred", "<SMTP_CRED>"), ("smtp-name", "IT mailbox SMTP"), ("table-id", "<AUDIT_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-google-guest-apply.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.google_cred, a.google_name, a.smtp_cred, a.smtp_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
