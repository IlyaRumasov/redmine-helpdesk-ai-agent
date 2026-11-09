"""Збирає workflows/support-agent-ad-account.json (флоу «Автоматизатор: AD Account & Personal Mail»).

Два сценарії з HelpDesk:
  * «Зміна пароля ...»: якщо обліковий запис AD вимкнено через неактивність, вмикає його і дає посилання на портал скидання пароля;
  * «Зміна особистої пошти»: готує зміну postOfficeBox і просить підтвердження (виконує флоу build_apply.py після коментаря «Погоджую» від IT-адміна).
Паролів флоу не бачить і не надсилає. Усе інше передає групі «IT Адміни».

Запуск: python tools/build_ad.py [--redmine-cred ID --redmine-name NAME --ldap-cred ID --ldap-name NAME --table-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = "https://reset-password.example.com/"

PARSE_JS = r"""
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
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const allowed = String(cfg.allowed_author_ids).split(',').map(s => s.trim()).filter(Boolean);
const everyone = allowed.includes('*');  // '*' у Config = заявки від усіх авторів (тест)
// у бойовому режимі заявки, яких торкався лише DRY RUN, обробляються заново
const seen = new Set($('[Data Table] Get Processed').all()
  .filter(i => i.json.kind !== 'dispatch' && (dry || i.json.dry_run === false)).map(i => String(i.json.ticket_id)));
const OWN = /@(example|example)\.(com|tech|us|eu)$/;
const DOMAIN = /\.(com|net|org|tech|us|eu|io|ua|ai|dev|ru|pl)$/;
const out = [];
for (const t of ($input.first().json.issues || [])) {
  // група «IT Адміни» (admins_group_id) = виконавець за замовчуванням, заявка ще нерозподілена
  if ((t.assigned_to && String(t.assigned_to.id) !== String(cfg.admins_group_id)) || seen.has(String(t.id))) continue;
  const text = `${t.subject}\n${t.description || ''}`.toLowerCase();
  let kind = null;
  if (/змін\S*\s+особист\S*\s+пошт/.test(text)) kind = 'mail';
  else if (/(змін\S*\s+парол|скин\S*\s+парол|парол\S*\s+vpn)/.test(text)) kind = 'enable';
  // блокування після невдалих входів (AD, SOGo, VPN): що саме робити, вирішує стан обліковки в AD
  else if (/(заблок|розблок|locked out|account locked)/.test(text) && !/(redmine|редмайн|google|гугл|gmail|rivneshare|1с|1c)/.test(text)) kind = 'enable';
  if (!kind && t.ai) kind = t.ai.type === 'mail_change' ? 'mail' : t.ai.type === 'password_reset' ? 'enable' : null;
  if (!kind) continue;
  const base = { ticket_id: t.id, salute: saluteOf(t), kind, login: null, ticket_mail: null, old_mail: null, new_mail: null, reason: '' };
  const esc = reason => out.push({ ...base, kind: 'escalate', request: kind, reason });
  if (!(everyone || allowed.includes(String(t.author && t.author.id)))) { esc('автор задачі не входить до списку координаторів'); continue; }
  const mails = [...new Set(text.match(/[\w.+-]+@[\w-]+(\.[\w-]+)+/g) || [])];
  const ext = mails.filter(m => !OWN.test(m));
  const clean = text.replace(/https?:\/\/\S+/g, ' ').replace(/[\w.+-]+@[\w.-]+/g, ' ');
  const own = mails.filter(m => OWN.test(m)).map(m => m.split('@')[0]);
  let logins = [...new Set((clean.match(/[a-z0-9][a-z0-9._-]*[a-z0-9]/g) || []).concat(own)
    .filter(w => /[._]/.test(w) && /[a-z].*[a-z]/.test(w) && !DOMAIN.test(w)))];
  if (!logins.length && t.ai) logins = [...new Set((t.ai.logins || []).map(x => String(x).toLowerCase()).filter(x => /^[a-z0-9][a-z0-9._-]*[a-z0-9]$/.test(x) && /[._]/.test(x)))];
  if (logins.length !== 1) { esc(logins.length ? `у заявці кілька логінів (${logins.join(', ')})` : 'не знайшов у заявці логіну AD'); continue; }
  base.login = logins[0];
  // логін із заявки часто скопійований з іншої системи (mdy_x_y у Redmine, в AD може бути x.y чи ms.x.y): готуємо варіанти й пару ім'я/прізвище для пошуку
  const rest = base.login.replace(/^(mdy|ms)[._]/, '');
  const parts = rest.split(/[._]/).filter(Boolean);
  base.cands = [...new Set([rest, parts.join('.'), 'ms.' + parts.join('.')])].filter(c => c !== base.login);
  base.pair = parts.length === 2 ? parts : null;
  if (kind === 'enable') {
    if (ext.length > 1) { esc('у заявці кілька особистих адрес'); continue; }
    base.ticket_mail = ext[0] || null;
  } else {
    // стару адресу ГА часто вже не має, тож достатньо нової; зміну підтверджує адміністратор
    const m = text.match(/(?:із|з|from)\s+([\w.+-]+@[\w-]+(?:\.[\w-]+)+)\s+(?:на|to)\s+([\w.+-]+@[\w-]+(?:\.[\w-]+)+)/);
    if (m) { base.old_mail = m[1]; base.new_mail = m[2]; }
    else if (ext.length === 1) base.new_mail = ext[0];
    else { esc(ext.length ? 'у заявці кілька адрес, не зрозуміло, яка нова' : 'у заявці немає нової особистої адреси'); continue; }
  }
  out.push(base);
}
// Слоти для пошуку в AD (нода LDAP не приймає фільтр цілком із виразу): логіни з заявок і їхні варіанти, до 16; 4 пари ім'я/прізвище; зайві = __none__
const san = x => String(x).replace(/[*()\\\u0000]/g, '');
const logs = out.filter(x => x.login);
const SL = [...new Set(logs.map(x => x.login).concat(logs.flatMap(x => x.cands)).map(san).filter(Boolean))].slice(0, 16);
const PG = [], PS = [];
for (const x of logs) if (x.pair && PG.length < 4) { PG.push(san(x.pair[0])); PS.push(san(x.pair[1])); }
while (SL.length < 16) SL.push('__none__');
while (PG.length < 4) { PG.push('__none__'); PS.push('__none__'); }
if (out.length) { out[0].slots = SL; out[0].pg = PG; out[0].ps = PS; }
return out.map(json => ({ json }));
"""

FILTER = "=(&(objectClass=user)(|" + "".join("(sAMAccountName={{ $('Code: Parse Tickets').first().json.slots[%d] }})" % i for i in range(16)) + "".join("(&(givenName={{ $('Code: Parse Tickets').first().json.pg[%d] }})(sn={{ $('Code: Parse Tickets').first().json.ps[%d] }}))" % (i, i) for i in range(4)) + "))"

DECIDE_JS = r"""
const cfg = $('Config').first().json;
const dry = cfg.dry_run === true || cfg.dry_run === 'true';
const scope = ',' + String(cfg.scope_dn).toLowerCase();
const protectedGroups = String(cfg.protected_groups).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const one = v => Array.isArray(v) ? v[0] : v;
const ad = $input.all().map(i => i.json).filter(j => j.sAMAccountName);
return $('Code: Parse Tickets').all().map(({ json: t }) => {
  const r = { ...t, dry_run: dry, action: 'escalate', dn: null, new_uac: null };
  if (t.kind === 'escalate') return { json: r };
  const esc = reason => ({ json: { ...r, reason } });
  const low = v => String(one(v) || '').toLowerCase();
  const byLogin = l => ad.filter(j => low(j.sAMAccountName) === l);
  let hits = byLogin(t.login), via = 'exact';
  if (!hits.length) { hits = (t.cands || []).flatMap(byLogin); via = 'variant'; }
  if (!hits.length && t.pair) { const [g, s] = t.pair; hits = ad.filter(j => (low(j.givenName) === g && low(j.sn) === s) || (low(j.givenName) === s && low(j.sn) === g)); via = 'name'; }
  if (hits.length !== 1) return esc(hits.length ? 'кілька збігів в AD' : `логін ${t.login} не знайдено в AD (перевірено ${[t.login].concat(t.cands || []).join(', ')}${t.pair ? ' і пошук за іменем ' + t.pair.join(' ') : ''}; можливо, обліковий запис видалено)`);
  const u = hits[0];
  if (via !== 'exact') { r.login_requested = t.login; r.login = low(u.sAMAccountName); }
  // знайдено лише за іменем: дію не виконуємо, показуємо інженеру, який обліковий запис підходить
  if (via === 'name') return esc(`логін ${t.login} не знайдено, але в AD за іменем є ${r.login}: інженер має перевірити, що це та сама людина`);
  const dn = String(u.dn);
  r.dn = dn;
  if (!dn.toLowerCase().endsWith(scope)) return esc(`обліковий запис поза дозволеним OU (${dn})`);
  const groups = [].concat(u.memberOf || []).map(g => String(g).toLowerCase());
  if (groups.some(g => protectedGroups.some(p => g.startsWith('cn=' + p + ',')))) return esc('обліковий запис у привілейованій групі, змінює тільки людина');
  const uac = parseInt(one(u.userAccountControl), 10) || 0;
  const pobox = String(one(u.postOfficeBox) || '').trim().toLowerCase();
  if (t.kind === 'enable') {
    // вимкнений (неактивність) -> увімкнути; заблокований після невдалих входів (lockoutTime > 0) -> зняти блокування
    const disabled = !!(uac & 2);
    const locked = (Number(one(u.lockoutTime)) || 0) > 0;
    if (!disabled && !locked) return esc('обліковий запис активний і не заблокований, потрібні нові дані для входу (це робить людина)');
    if (disabled && !pobox) return esc('в AD немає особистої пошти, посилання на скидання пароля нікуди не прийде');
    if (t.ticket_mail && t.ticket_mail !== pobox) return esc('пошта в заявці не збігається з особистою поштою в AD');
    r.action = disabled ? 'enable' : 'unlock'; r.new_uac = String(uac & ~2); r.was_locked = locked;
  } else {
    if (uac & 2) return esc('обліковий запис вимкнено, спершу потрібна перевірка людиною');
    if (t.new_mail === pobox) return esc('нова адреса така сама, як в AD');
    r.current_mail = pobox;
    r.old_mismatch = !!(t.old_mail && t.old_mail !== pobox);
    r.action = 'request_approval';
  }
  return { json: r };
});
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
const portal = cfg.portal_url;
return $input.all().map(({ json: j }) => {
  let note, noteEn = '', kind = j.action, close = false, assign = false, approval = false, payload = '', details = j.reason || '';
  const failed = j.done === 'no';
  if (j.action === 'unlock' && !failed) {
    close = true;
    note = `Обліковий запис ${j.login} розблоковано (його було заблоковано після кількох невдалих спроб входу). ГА може увійти зі своїм паролем; якщо пароль забуто, скористайтеся порталом ${portal}`;
    noteEn = `Account ${j.login} has been unlocked (it was locked after several failed sign-in attempts). You can sign in with your password; if you have forgotten it, use the portal ${portal}`;
  } else if (j.action === 'enable' && !failed) {
    close = true;
    note = `Ми успішно увімкнули обліковий запис ${j.login}. Тепер ГА може самостійно змінити пароль, скориставшись сервісом за посиланням: ${portal}`;
    noteEn = `Your account ${j.login} has been enabled. You can now set a new password yourself using the service: ${portal}`;
  } else if (j.action === 'request_approval') {
    kind = 'set_mail'; assign = true; approval = true;
    payload = JSON.stringify({ login: j.login, dn: j.dn, old: j.current_mail, new: j.new_mail });
    details = `${j.current_mail || '(порожньо)'} -> ${j.new_mail}`;
    note = `Агент підготував зміну особистої пошти (поле «Почтовый ящик» в AD) для ${j.login}: ${details}.`
      + (j.old_mismatch ? ` Увага: стара адреса в заявці (${j.old_mail}) не збігається з AD.` : '')
      + ' Щоб виконати, учасник «IT Адміни» пише в задачі коментар «Погоджую», агент змінить пошту й закриє задачу. Щоб відхилити, закрийте задачу або нічого не пишіть (через 7 днів зміну буде скасовано).';
  } else {
    kind = 'escalate'; assign = true;
    if (failed) details = `помилка AD під час дії «${j.action}»: ${j.error}`;
    note = `Агент: автоматично не обробляю. Причина: ${details}. Передано адміністраторам.`;
  }
  if (j.login_requested && j.action !== 'escalate') note += ` Примітка: у заявці вказано логін «${j.login_requested}», в AD знайдено «${j.login}».`;
  if (j.login_requested && j.action !== 'escalate') noteEn += ` Note: the ticket mentions login "${j.login_requested}", but "${j.login}" was found in AD.`;
  if (j.dry_run) {
    const would = j.action === 'enable' ? `увімкнув би ${j.login} і дав би посилання на портал`
      : j.action === 'unlock' ? `зняв би блокування ${j.login} після невдалих входів і закрив би задачу`
      : j.action === 'request_approval' ? `передав би на підтвердження зміну особистої пошти ${j.login}: ${details}`
      : `передав би задачу адміністраторам. Причина: ${details}`;
    note = `DRY RUN (змін не внесено). Агент ${would}.`;
    close = false; assign = false;  // approval лишається: сухий рядок awaiting_approval дозволяє флоу Apply показати «виконав би»
  }
  return { json: { ticket_id: j.ticket_id, kind, note: (j.dry_run || !['enable', 'unlock'].includes(kind)) ? note : polite(note, noteEn, j.ticket_id), private: j.dry_run || !['enable', 'unlock'].includes(kind), close, assign, approval,
                   logins: j.login || '', outcome: approval ? 'awaiting_approval' : kind, details, payload,
                   dry_run: j.dry_run, at: new Date().toISOString() } };
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
    cond = lambda cid, expr: {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                                             "conditions": [{"id": cid, "leftValue": expr, "rightValue": "",
                                                             "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                                             "combinator": "and"}}
    mark = lambda done: {"mode": "raw", "jsonOutput": "={{ JSON.stringify({ ...$('Code: Decide AD Action').item.json, done: '%s', error: %s }) }}"
                         % (done, "String($json.error || 'невідома помилка')" if done == "no" else "''"), "options": {}}

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: AD Account & Personal Mail\n"
        "Кожні 5 хв бере нові нерозподілені задачі HelpDesk:\n"
        "* «Зміна пароля»: вимкнений обліковий запис AD -> увімкнути + посилання на портал скидання пароля;\n"
        "* «Зміна особистої пошти»: готує зміну postOfficeBox і просить підтвердження; змінює флоу «Apply Approved Changes» після коментаря «Погоджую» від адміна.\n"
        "Паролі не бачить і не надсилає. Лише в межах OU з Config.scope_dn. Решту передає «IT Адміни».\n\n"
        "**DRY_RUN** (Config): true = нічого не змінює, лише приватна нотатка і запис в аудит."), "height": 230, "width": 600}, -200, -300)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "dry_run", "value": True, "type": "boolean"},
        {"id": "a2", "name": "project_id", "value": "<PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "tracker_id", "value": "<TRACKER_ID>", "type": "string"},
        {"id": "a4", "name": "allowed_author_ids", "value": "<COORDINATOR_IDS>", "type": "string"},
        {"id": "a5", "name": "scope_dn", "value": "OU=AllUsers,OU=ORG,DC=example,DC=com", "type": "string"},
        {"id": "a6", "name": "protected_groups", "value": "Domain Admins,Enterprise Admins,Administrators,Schema Admins", "type": "string"},
        {"id": "a7", "name": "portal_url", "value": PORTAL, "type": "string"},
        {"id": "a8", "name": "resolved_status_id", "value": "17", "type": "string"},
        {"id": "a9", "name": "admins_group_id", "value": "35", "type": "string"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Processed", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get New Tickets", RM, {"operation": "getAll", "returnAll": True, "filters": {
         "project_id": cfgv("project_id"), "tracker_id": cfgv("tracker_id"), "status_id": "1"}, "options": {}}, 660, 0, credentials=rmc)
    node("Code: Parse Tickets", "n8n-nodes-base.code", {"jsCode": PARSE_JS.strip()}, 880, 0, ver=2)
    node("[AD] Search Users", "n8n-nodes-base.ldap", {"baseDN": "DC=example,DC=com", "searchFor": "custom", "customFilter": FILTER,
         "returnAll": True, "options": {"scope": "sub", "attributes": ["sAMAccountName", "givenName", "sn", "userAccountControl", "postOfficeBox", "memberOf", "lockoutTime"]}},
         1100, 0, credentials=ldc, executeOnce=True, alwaysOutputData=True)
    node("Code: Decide AD Action", "n8n-nodes-base.code", {"jsCode": DECIDE_JS.strip()}, 1320, 0, ver=2)
    node("Route by Action", "n8n-nodes-base.switch", {"rules": {"values": [
        {**cond("r1", "={{ ['enable', 'unlock'].includes($json.action) && !$json.dry_run }}"), "renameOutput": True, "outputKey": "restore access"},
]},
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": "report only"}}, 1540, 0, ver=3.2)
    # увімкнути (зняти ACCOUNTDISABLE, решта прапорців без змін) і скинути блокування після невдалих входів
    node("[AD] Restore Access", "n8n-nodes-base.ldap", {"operation": "update", "dn": "={{ $json.dn }}",
         "attributes": {"replace": [{"id": "userAccountControl", "value": "={{ $json.new_uac }}"}, {"id": "lockoutTime", "value": "0"}]}},
         1760, -260, credentials=ldc, onError="continueErrorOutput")
    node("Mark Done", "n8n-nodes-base.set", mark("yes"), 1980, -200, ver=3.4)
    node("Mark Failed", "n8n-nodes-base.set", mark("no"), 1980, -60, ver=3.4)
    node("Merge Results", "n8n-nodes-base.merge", {"numberInputs": 3}, 2200, 0, ver=3.2)
    node("Code: Build Report", "n8n-nodes-base.code", {"jsCode": REPORT_JS.strip()}, 2420, 0, ver=2)
    node("[Redmine] Update Ticket", RM, {"operation": "update", "issueId": "={{ $json.ticket_id }}", "notes": "={{ $json.note }}",
         "private_notes": "={{ $json.private }}",
         "additionalFields": {"customFields": {"field": [{"id": "92", "value": "={{ $json.close ? 'Восстановление доступа' : '' }}"}]},
                              "status_id": "={{ $json.close ? $('Config').first().json.resolved_status_id : undefined }}",
                              "assigned_to_id": "={{ $json.assign ? $('Config').first().json.admins_group_id : undefined }}"},
         "options": {}}, 2640, 0, credentials=rmc, onError="continueRegularOutput")
    cols = [("ticket_id", "number"), ("kind", "string"), ("logins", "string"), ("outcome", "string"), ("dry_run", "boolean"), ("details", "string"), ("at", "string"), ("payload", "string")]
    node("[Data Table] Log Result", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [],
        "value": {c: "={{ $('Code: Build Report').item.json.%s }}" % c for c, _ in cols},
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 2860, 0, ver=1.1)

    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Processed"), ("[Data Table] Get Processed", "[Redmine] Get New Tickets"),
                 ("[Redmine] Get New Tickets", "Code: Parse Tickets"), ("Code: Parse Tickets", "[AD] Search Users"),
                 ("[AD] Search Users", "Code: Decide AD Action"), ("Code: Decide AD Action", "Route by Action"),
                 ("Merge Results", "Code: Build Report"), ("Code: Build Report", "[Redmine] Update Ticket"),
                 ("[Redmine] Update Ticket", "[Data Table] Log Result")]:
        link(a, b)
    link("Route by Action", "[AD] Restore Access", 0)
    link("Route by Action", "Merge Results", 1, 2)
    link("[AD] Restore Access", "Mark Done", 0)
    link("[AD] Restore Access", "Mark Failed", 1)
    link("Mark Done", "Merge Results", 0, 0)
    link("Mark Failed", "Merge Results", 0, 1)
    return {"name": "Автоматизатор: AD Account & Personal Mail", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--redmine-cred", default="<REDMINE_CRED>")
    ap.add_argument("--redmine-name", default="Redmine <ENV> account")
    ap.add_argument("--ldap-cred", default="<LDAP_CRED>")
    ap.add_argument("--ldap-name", default="LDAP service account")
    ap.add_argument("--table-id", default="<AUDIT_TABLE>")
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-ad-account.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.ldap_cred, a.ldap_name, a.table_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
