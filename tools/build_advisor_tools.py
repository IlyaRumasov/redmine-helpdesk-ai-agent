"""Збирає під-воркфлоу «інструменти радника» (етап 1): лише читання, білі списки полів на виході.

advisor_ticket        історія заявки (Redmine)
advisor_similar       схожі вирішені заявки з бази знань (Qdrant it_support_kb)
advisor_policies      політики та інструкції (Qdrant policies_kb, лише категорії policy і howto)
advisor_ad_user       стан облікового запису AD: увімкнено, заблоковано, OU, дати; без груп, паролів, пошти
advisor_google_guest  стан гостьового акаунта Google: існує, призупинено, 2FA, дати; без паролів і кодів
advisor_glpi          техніка за користувачем у GLPI: тип, назва, інв. номер, статус

Кожен інструмент перевіряє формат входу в коді, робить лише GET/search і повертає фіксований набір полів.
Запуск: python tools/build_advisor_tools.py  (пише workflows/advisor-*.json із заглушками)
"""
import copy, json, re, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLPI_JS = Path(r"C:\Claude\staff-offboarding\src\glpi.js")

QDRANT = "http://<QDRANT_HOST>:6333"
CODE = "n8n-nodes-base.code"


class Flow:
    def __init__(self, name):
        self.name, self.nodes, self.conn = name, [], {}
        self.prev = None

    def add(self, name, type_, params, ver=1, credentials=None, chain=True, y=0, **extra):
        x = 220 * len(self.nodes)
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        if credentials:
            n["credentials"] = credentials
        n.update(extra)
        self.nodes.append(n)
        if chain and self.prev:
            self.conn.setdefault(self.prev, {"main": [[]]})["main"][0].append({"node": name, "type": "main", "index": 0})
        self.prev = name
        return name

    def trigger(self, fields):
        self.add("When Called by Agent", "n8n-nodes-base.executeWorkflowTrigger",
                 {"workflowInputs": {"values": [{"name": f} for f in fields]}}, ver=1.2, chain=False)

    def code(self, name, js):
        self.add(name, CODE, {"jsCode": js.strip()}, ver=2)

    def config(self, assignments):
        self.add("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
            {"id": f"a{i}", "name": k, "value": v, "type": "string"} for i, (k, v) in enumerate(assignments.items())]}, "options": {}}, ver=3.4)

    def export(self, note):
        self.nodes.append({"parameters": {"content": note, "height": 160, "width": 520}, "type": "n8n-nodes-base.stickyNote", "typeVersion": 1,
                           "position": [-60, -260], "id": str(uuid.uuid4()), "name": "Sticky Note"})
        return {"name": self.name, "nodes": self.nodes, "connections": self.conn, "settings": {"executionOrder": "v1"}}


def http_json(name, method, url, cred_type, cred, body_expr=None):
    p = {"method": method, "url": url, "authentication": "predefinedCredentialType", "nodeCredentialType": cred_type,
         "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}
    if body_expr:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body_expr})
    return name, "n8n-nodes-base.httpRequest", p, cred


# ---------- 1. історія заявки ----------
def ticket_tool(rm_id, rm_name):
    f = Flow("Радник (інструмент): Ticket History")
    f.trigger(["ticket_id"])
    f.config({"project_id": "<PROJECT_ID>", "text_limit": "2500", "note_limit": "800"})
    f.code("Code: Validate", r"""
const id = String($('When Called by Agent').first().json.ticket_id || '').trim();
return [{ json: { ticket_id: /^\d{1,9}$/.test(id) ? id : '0' } }];""")
    f.add("[Redmine] Get Ticket", "n8n-nodes-redmine.redmine", {"operation": "get", "issueId": "={{ $json.ticket_id }}", "include": ["journals"], "options": {}},
          credentials={"redmineApi": {"id": rm_id, "name": rm_name}}, onError="continueRegularOutput", alwaysOutputData=True)
    f.code("Code: Result", r"""
const cfg = $('Config').first().json;
const j = $input.first().json || {};
const t = j.issue || j;
if (!t.id) return [{ json: { ok: false, error: 'заявку не знайдено або немає доступу' } }];
if (String(t.project && t.project.id) !== String(cfg.project_id)) return [{ json: { ok: false, error: 'заявка не з проєкту HelpDesk' } }];
const cut = (s, n) => String(s || '').replace(/\r/g, '').slice(0, n);
const notes = (t.journals || []).filter(x => (x.notes || '').trim()).slice(-30)
  .map(x => ({ on: x.created_on, private: !!x.private_notes, text: cut(x.notes, Number(cfg.note_limit)) }));
return [{ json: { ok: true, id: t.id, subject: cut(t.subject, 300), description: cut(t.description, Number(cfg.text_limit)),
  status: t.status && t.status.name, created: t.created_on, notes } }];""")
    return f.export("## Advisor Tool: Ticket History\nЛише читання однієї заявки HelpDesk. На виході: тема, опис, статус, коментарі (до 30). Без вкладень, власних полів і даних авторів.")


# ---------- 2-3. пошук у Qdrant ----------
def qdrant_search_tool(name, collection, gem_cred, flt, label, extra_note):
    f = Flow(name)
    f.trigger(["query"])
    f.code("Code: Prepare", r"""
const q = String($('When Called by Agent').first().json.query || '').replace(/\s+/g, ' ').trim().slice(0, 1500);
return [{ json: { q, ok: q.length >= 3 } }];""")
    n, t, p, c = http_json("[Gemini] Embed Query", "POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent",
                           "googlePalmApi", {"googlePalmApi": gem_cred},
                           "={{ JSON.stringify({ model: 'models/gemini-embedding-001', content: { parts: [{ text: $json.q || 'x' }] }, taskType: 'RETRIEVAL_QUERY' }) }}")
    f.add(n, t, p, ver=4.2, credentials=c, onError="continueRegularOutput")
    kb = collection.startswith("it_support_kb")
    if kb:  # одним батч-запитом: 3 схожі заявки (без плейбуків) + 2 найкращі затверджені плейбуки
        pb = json.dumps({"key": "metadata.source", "match": {"value": "playbook"}})
        f.code("Code: Search Body", ("const body = ($input.first().json.body || {});\nconst v = (body.embedding || {}).values;\n"
                                     "if (!v) throw new Error('немає вектора запиту');\n"
                                     "const pb = %s;\n"
                                     "return [{ json: { search: { searches: [\n"
                                     "  { vector: v, limit: 3, with_payload: true, filter: { must_not: [pb] } },\n"
                                     "  { vector: v, limit: 3, with_payload: true, filter: { must: [pb] } } ] } } }];") % pb)
    else:
        f.code("Code: Search Body", ("const body = ($input.first().json.body || {});\nconst v = (body.embedding || {}).values;\n"
                                     "if (!v) throw new Error('немає вектора запиту');\n"
                                     "return [{ json: { search: { vector: v, limit: 4, with_payload: true%s } } }];") % (", filter: " + json.dumps(flt) if flt else ""))
    n, t, p, c = http_json("[Qdrant] Search", "POST", f"{QDRANT}/collections/{collection}/points/search" + ("/batch" if kb else ""), "qdrantApi",
                           {"qdrantApi": {"id": "<QDRANT_CRED_ID>", "name": "<QDRANT_CRED_NAME>"}}, "={{ JSON.stringify($json.search) }}")
    f.add(n, t, p, ver=4.2, credentials=c, onError="continueRegularOutput")
    pick = ("{ ticket: m.ticket, score: r.score, text: String(r.payload.content || '').slice(0, 900) }" if collection.startswith("it_support_kb")
            else "{ file: String(m.fileName || '').slice(0, 120), category: m.category, score: r.score, text: String(r.payload.content || '').slice(0, 900) }")
    if kb:
        f.code("Code: Result", ("if (!$('Code: Prepare').first().json.ok) return [{ json: { ok: false, error: 'запит надто короткий' } }];\n"
                                "const b = $input.first().json.body || {};\nif (!Array.isArray(b.result) || b.result.length < 2) return [{ json: { ok: false, error: 'пошук недоступний' } }];\n"
                                "const pick = r => { const m = (r.payload || {}).metadata || {}; return %s; };\n"
                                "return [{ json: { ok: true, results: (b.result[0] || []).map(pick), approved_playbooks: (b.result[1] || []).map(pick) } }];") % pick)
    else:
        f.code("Code: Result", ("if (!$('Code: Prepare').first().json.ok) return [{ json: { ok: false, error: 'запит надто короткий' } }];\n"
                                "const b = $input.first().json.body || {};\nif (!Array.isArray(b.result)) return [{ json: { ok: false, error: 'пошук недоступний' } }];\n"
                                "return [{ json: { ok: true, results: b.result.map(r => { const m = (r.payload || {}).metadata || {}; return %s; }) } }];") % pick)
    return f.export(f"## Advisor Tool: {label}\n{extra_note}")


# ---------- 4. AD ----------
def ad_tool(ldap_id, ldap_name):
    f = Flow("Радник (інструмент): AD User Status")
    f.trigger(["login"])
    f.config({"scope_dn": "OU=AllUsers,OU=ORG,DC=example,DC=com", "protected_groups": "Domain Admins,Enterprise Admins,Administrators,Schema Admins"})
    f.code("Code: Validate", r"""
const l = String($('When Called by Agent').first().json.login || '').trim().toLowerCase();
return [{ json: { login: /^[a-z0-9][a-z0-9._-]{1,40}$/.test(l) ? l : '__none__' } }];""")
    f.add("[AD] Search User", "n8n-nodes-base.ldap", {"baseDN": "DC=example,DC=com", "searchFor": "custom",
          "customFilter": "=(&(objectClass=user)(sAMAccountName={{ $json.login }}))", "returnAll": True,
          "options": {"scope": "sub", "attributes": ["sAMAccountName", "userAccountControl", "lockoutTime", "whenCreated", "lastLogonTimestamp", "memberOf", "postOfficeBox"]}},
          credentials={"ldap": {"id": ldap_id, "name": ldap_name}}, alwaysOutputData=True)
    f.code("Code: Result", r"""
const cfg = $('Config').first().json;
const login = $('Code: Validate').first().json.login;
const one = v => Array.isArray(v) ? v[0] : v;
const u = $input.all().map(i => i.json).find(j => j.sAMAccountName && String(one(j.sAMAccountName)).toLowerCase() === login);
if (login === '__none__') return [{ json: { ok: false, error: 'некоректний формат логіна' } }];
if (!u) return [{ json: { ok: true, found: false } }];
const dn = String(u.dn || '');
const scope = ',' + String(cfg.scope_dn).toLowerCase();
const prot = String(cfg.protected_groups).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const groups = [].concat(u.memberOf || []).map(g => String(g).toLowerCase());
if (!dn.toLowerCase().endsWith(scope) || groups.some(g => prot.some(p => g.startsWith('cn=' + p + ',')))) {
  return [{ json: { ok: true, found: true, restricted: true, note: 'обліковий запис поза дозволеною зоною або привілейований: подробиці не показуємо' } }];
}
const uac = parseInt(one(u.userAccountControl), 10) || 0;
const day = s => { const m = String(s || '').match(/^(\d{4})(\d{2})(\d{2})/); return m ? `${m[1]}-${m[2]}-${m[3]}` : ''; };
const ft = v => { const n = Number(one(v)); return n > 0 ? new Date(n / 10000 - 11644473600000).toISOString().slice(0, 10) : ''; };
const ou = dn.split(',').filter(p => /^ou=/i.test(p)).map(p => p.slice(3)).reverse().join(' / ');
return [{ json: { ok: true, found: true, restricted: false, enabled: !(uac & 2), locked: (Number(one(u.lockoutTime)) || 0) > 0,
  ou, created: day(one(u.whenCreated)), last_logon: ft(u.lastLogonTimestamp), personal_mail_set: !!String(one(u.postOfficeBox) || '').trim() } }];""")
    return f.export("## Advisor Tool: AD User Status\nЛише пошук за точним логіном. Видає: увімкнено, заблоковано, OU, дати створення й останнього входу, чи заповнена особиста пошта (так/ні). Групи, паролі й адреси не повертаються; поза дозволеним OU або в привілейованій групі повертається лише «restricted».")


# ---------- 5. Google guest ----------
def google_tool(g_id, g_name):
    f = Flow("Радник (інструмент): Google Guest Status")
    f.trigger(["login"])
    f.config({"work_mail_domain": "example.com", "guest_domain": "guests.example.com"})
    f.code("Code: Validate", r"""
const cfg = $('Config').first().json;
const l = String($('When Called by Agent').first().json.login || '').trim().toLowerCase();
const ok = /^[a-z0-9][a-z0-9._-]{1,40}$/.test(l);
return [{ json: { login: l, ok, guest: ok ? `${l}_${cfg.work_mail_domain}@${cfg.guest_domain}.guest.google` : 'none@invalid' } }];""")
    f.add("[Google] Get Guest", "n8n-nodes-base.httpRequest", {"method": "GET",
          "url": "=https://admin.googleapis.com/admin/directory/v1/users/{{ encodeURIComponent($json.guest) }}?projection=basic",
          "authentication": "predefinedCredentialType", "nodeCredentialType": "googleApi",
          "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}},
          ver=4.2, credentials={"googleApi": {"id": g_id, "name": g_name}}, onError="continueRegularOutput")
    f.code("Code: Result", r"""
const cfg = $('Config').first().json;
const v = $('Code: Validate').first().json;
if (!v.ok) return [{ json: { ok: false, error: 'некоректний формат логіна' } }];
const r = $input.first().json;
if (Number(r.statusCode) === 404 || Number(r.statusCode) === 403) return [{ json: { ok: true, found: false } }];
if (Number(r.statusCode) !== 200) return [{ json: { ok: false, error: 'Google відповів ' + r.statusCode } }];
const b = r.body || {};
const owner = String((b.guestAccountInfo || {}).primaryGuestEmail || '').toLowerCase();
const day = s => String(s || '').slice(0, 10);
const never = !b.lastLoginTime || String(b.lastLoginTime).startsWith('1970');
return [{ json: { ok: true, found: true, suspended: !!b.suspended, enrolled_2sv: !!b.isEnrolledIn2Sv, created: day(b.creationTime),
  last_login: never ? '' : day(b.lastLoginTime), never_logged_in: never,
  owner_matches_login: owner === `${v.login}@${cfg.work_mail_domain}` } }];""")
    return f.export("## Advisor Tool: Google Guest Status\nЛише читання одного гостьового акаунта за логіном. Видає: існує, призупинено, налаштована 2FA, дати створення й останнього входу, чи прив'язка збігається з робочою поштою. Паролів і резервних кодів немає.")


# ---------- 6. GLPI ----------
def glpi_tool(glpi_cred_id, glpi_cred_name, glpi_url):
    lib = GLPI_JS.read_text(encoding="utf-8").replace("<GLPI_URL>", glpi_url)
    f = Flow("Радник (інструмент): GLPI Equipment")
    f.trigger(["login"])
    f.code("Code: Urls", lib + r"""
const l = String($('When Called by Agent').first().json.login || '').trim().toLowerCase();
const ok = /^[a-z0-9][a-z0-9._-]{1,40}$/.test(l);
const u = ok ? glpiUrls(l, [], []) : { user_url: '', assets_url: '' };
return [{ json: { login: l, ok, ...u } }];""")
    cred = {"httpCustomAuth": {"id": glpi_cred_id, "name": glpi_cred_name}}
    base = {"authentication": "genericCredentialType", "genericAuthType": "httpCustomAuth",
            "options": {"response": {"response": {"fullResponse": True, "neverError": True}}}}
    f.add("[GLPI] Open Session", "n8n-nodes-base.httpRequest", {**base, "url": f"{glpi_url}/apirest.php/initSession"}, ver=4.2, credentials=cred)
    hdr = {"sendHeaders": True, "headerParameters": {"parameters": [{"name": "Session-Token", "value": "={{ ($('[GLPI] Open Session').first().json.body || {}).session_token || '' }}"}]}}
    f.add("[GLPI] Find User", "n8n-nodes-base.httpRequest", {**base, **hdr, "url": "={{ $('Code: Urls').first().json.user_url }}"}, ver=4.2, credentials=cred, onError="continueRegularOutput")
    f.add("[GLPI] Find Assets", "n8n-nodes-base.httpRequest", {**base, **hdr, "url": "={{ $('Code: Urls').first().json.assets_url }}"}, ver=4.2, credentials=cred, onError="continueRegularOutput")
    f.code("Code: Result", lib + r"""
const v = $('Code: Urls').first().json;
if (!v.ok) return [{ json: { ok: false, error: 'некоректний формат логіна' } }];
const res = glpiCheck(v.login, [], [], $('[GLPI] Open Session').first().json, $('[GLPI] Find User').first().json, $('[GLPI] Find Assets').first().json);
if (res.status === 'error') return [{ json: { ok: false, error: 'GLPI недоступний' } }];
// лише тип, назва, інвентарний номер, статус: серійні номери й вільний текст «контакт» не віддаємо
const clean = s => String(s).replace(/, сер\. № [^,]*/g, '').replace(/, контакт: .*$/, '');
return [{ json: { ok: true, status: res.status, items: res.items.map(clean), possible_count: (res.possible || []).length, more: res.more || 0 } }];""")
    return f.export("## Advisor Tool: GLPI Equipment\nЛише читання (креди GLPI read-only). Видає статус (assigned / clear / check / not_in_glpi / ambiguous) і перелік: тип, назва, інв. номер, статус. Без серійних номерів і вільного тексту.")


def build_all(cfg):
    return {
        "advisor-ticket": ticket_tool(*cfg["rm"]),
        "advisor-similar": qdrant_search_tool("Радник (інструмент): Similar Tickets", "it_support_kb", cfg["gem"], None, "Similar Tickets",
                                              "Пошук за змістом у базі обезличених вирішених заявок (it_support_kb). Лише читання; одним запитом: 3 схожі заявки (results) і 2 затверджені плейбуки (approved_playbooks, ticket = playbook-N)."),
        "advisor-policies": qdrant_search_tool("Радник (інструмент): Policies", "policies_kb", cfg["gem"],
                                               {"must": [{"key": "metadata.category", "match": {"any": ["policy", "howto"]}}]}, "Policies",
                                               "Пошук за змістом у політиках та інструкціях (policies_kb). Фільтр категорій policy і howto зашитий у код, гайди клієнтів недоступні."),
        "advisor-ad-user": ad_tool(*cfg["ldap"]),
        "advisor-google-guest": google_tool(*cfg["google"]),
        "advisor-glpi": glpi_tool(*cfg["glpi"]),
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ph = ("<ID>", "<NAME>")
    flows = build_all({"rm": ph, "gem": {"id": "<GEMINI_CRED>", "name": "<NAME>"}, "ldap": ph, "google": ph, "glpi": ("<ID>", "<NAME>", "<GLPI_URL>")})
    (ROOT / "workflows").mkdir(exist_ok=True)
    for k, wf in flows.items():
        (ROOT / "workflows" / f"{k}.json").write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
        print(k, len(wf["nodes"]), "вузлів")
