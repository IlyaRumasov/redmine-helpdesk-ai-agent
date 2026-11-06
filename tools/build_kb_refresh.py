"""Збирає флоу «Сервіс: KB Weekly Refresh» (workflows/kb-weekly-refresh.json): щопонеділка додає в базу знань радника нові закриті заявки HelpDesk.

Ланцюжок: Schedule -> Config -> Redmine (100 останніх закритих заявок HelpDesk) -> Filter (оновлені за N днів) -> по кожній коментарі -> таблиця слів-імен ->
Code: Anonymize (порт tools/kb_anonymize.py) -> Gemini Embed -> Code: Build Points -> Qdrant Upsert (it_support_kb).
Id точок = uuid5 від номера заявки, як у kb_load.py: повторний запуск перезаписує, а не дублює. Плейбуки (source=playbook) не чіпає.
Слова для обезличення (імена та стоп-слова) лежать у таблиці n8n kb_name_tokens (word, kind=name|stop).

Запуск: python tools/build_kb_refresh.py  (пише workflows/kb-weekly-refresh.json із заглушками)
JS-бібліотека ANON_LIB тестується проти Python у tools/test_kb_refresh_parity.py.
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ANON_LIB = r"""
const W = String.raw`\p{L}\p{N}_`;
const BB = `(?:(?<=[${W}])(?![${W}])|(?<![${W}])(?=[${W}]))`;  // точний аналог \b у Python (Unicode)
const B0 = BB, B1 = BB;
const rx = (src, flags) => new RegExp(src, flags + 'u');
const CAP = String.raw`[А-ЯІЇЄҐ][а-яіїєґ'’-]{2,}`;
const IMG = rx(String.raw`!\S+\.(?:png|jpg|jpeg|gif)!|!clipboard-\S+!`, 'g');
const SIGN = rx(String.raw`\n-- ?\n[\s\S]*$`, '');
const QUOTE_HDR = rx(String.raw`^.*` + B0 + String.raw`wrote in #note-\d+:[ \t]*\n?`, 'gm');
const EMAIL = rx(`[${W}.+-]+@[${W}-]+(?:\\.[${W}-]+)+`, 'g');
const HANDLE = rx(`(?<![${W}.])@[A-Za-z0-9_]{3,}`, 'g');
const URLRX = rx(String.raw`https?://\S+`, 'g');
const DOMAIN = rx(B0 + String.raw`(?:[a-z0-9-]+\.)+(?:com|net|org|io|ua|us|ai|dev|tech|co|eu|info|biz|me|app|cloud|pl|de|ru)` + B1, 'gi');
const CLIENTS = rx(B0 + String.raw`(?:<CLIENT_NAME_A>|<CLIENT_NAME_B>)` + B1, 'gi');
const SUF3 = 'ович|евич|йович|ович|ич|івна|ївна|евна|овна|ична|ична', SUF2 = 'ович|евич|йович|ич|івна|ївна|евна|овна';
const FIO = rx(B0 + CAP + String.raw`\s+` + CAP + String.raw`\s+` + CAP + `(?:${SUF3})` + B1 + '|' + B0 + CAP + String.raw`\s+` + CAP + `(?:${SUF2})` + B1, 'g');
const IP = rx(B0 + String.raw`\d{1,3}(?:\.\d{1,3}){3}` + B1, 'g');
const PHONE = rx(`(?<![${W}])` + String.raw`\+?\d[\d\s().-]{8,}\d`, 'g');
const LOGIN = rx(B0 + String.raw`(?:ms|mdy)[._][a-z0-9][a-z0-9._-]*[a-z0-9]` + B1 + '|' + B0 + String.raw`[a-z][a-z0-9-]{0,20}[._][a-z][a-z0-9._-]{2,30}` + B1, 'gi');
const SAFE_DOTTED = rx(String.raw`\.(?:com|net|org|pdf|png|jpg|docx?|xlsx?|txt|json|csv|zip|exe|msi|local|google|io|ua|ai|tech)$`, 'i');
const BIGRAM = rx(B0 + '(' + CAP + ')' + String.raw`\s+(` + CAP + ')' + B1, 'g');
const CAPWORD = rx(B0 + String.raw`[А-ЯІЇЄҐA-Z][а-яіїєґa-z'’-]{2,}` + B1, 'g');
const GENERIC_DOMAINS = new Set(['google.com', 'gmail.com', 'microsoft.com', 'office.com', 'zoom.us', 'github.com', 'docs.google.com', 'meet.google.com', 'teams.microsoft.com']);
const cp = s => Array.from(s);
const cut = (s, n) => cp(s).slice(0, n).join('');

function makeCleaner(names, stop) {
  const hasNames = names.size > 0;
  const isName = w => {
    w = w.toLowerCase();
    const forms = [0, 1, 2, 3].filter(k => w.length - k >= 4).map(k => w.slice(0, w.length - k));
    if (forms.some(f => stop.has(f))) return false;  // часті слова (Redmine, Acme, Hello ...) іменами не вважаємо
    return forms.some(f => names.has(f));
  };
  const sub = (re, label, text, keep) => text.replace(re, m => (keep && keep(m)) ? m : label);
  return text => {
    text = String(text || '').replace(/\r/g, '');
    text = text.replace(IMG, '');
    text = text.replace(SIGN, '');
    text = text.replace(QUOTE_HDR, '');
    text = sub(EMAIL, '<EMAIL>', text);
    text = sub(HANDLE, '<HANDLE>', text);
    text = sub(URLRX, '<URL>', text);
    text = sub(DOMAIN, '<DOMAIN>', text, m => GENERIC_DOMAINS.has(m.toLowerCase()));
    text = sub(CLIENTS, '<CLIENT>', text);
    text = sub(FIO, '<PERSON>', text);
    text = sub(IP, '<IP>', text);
    text = sub(PHONE, '<PHONE>', text);
    text = sub(LOGIN, '<LOGIN>', text, m => SAFE_DOTTED.test(m));
    if (hasNames) text = text.replace(CAPWORD, m => isName(m) ? '<PERSON>' : m);
    text = text.replace(BIGRAM, (m, a, b) => (stop.has(a.toLowerCase()) && stop.has(b.toLowerCase())) ? m : '<PERSON>');
    return text.replace(/\n{3,}/g, '\n\n').trim();
  };
}

// uuid5 (SHA-1), щоб id точок збігались із Python (kb_load.py): uuid.uuid5(NS, str(ticket))
function sha1(bytes) {
  const rotl = (x, n) => (x << n) | (x >>> (32 - n));
  const ml = bytes.length * 8, padded = Array.from(bytes); padded.push(0x80);
  while (padded.length % 64 !== 56) padded.push(0);
  for (let i = 7; i >= 0; i--) padded.push(i >= 4 ? 0 : (ml >>> (i * 8)) & 255);
  let h0 = 0x67452301, h1 = 0xEFCDAB89, h2 = 0x98BADCFE, h3 = 0x10325476, h4 = 0xC3D2E1F0;
  for (let o = 0; o < padded.length; o += 64) {
    const w = new Array(80);
    for (let i = 0; i < 16; i++) w[i] = (padded[o + 4 * i] << 24) | (padded[o + 4 * i + 1] << 16) | (padded[o + 4 * i + 2] << 8) | padded[o + 4 * i + 3];
    for (let i = 16; i < 80; i++) w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999; } else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1; }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDC; } else { f = b ^ c ^ d; k = 0xCA62C1D6; }
      const t = (rotl(a, 5) + f + e + k + w[i]) | 0; e = d; d = c; c = rotl(b, 30); b = a; a = t;
    }
    h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0; h4 = (h4 + e) | 0;
  }
  return [h0, h1, h2, h3, h4].flatMap(x => [(x >>> 24) & 255, (x >>> 16) & 255, (x >>> 8) & 255, x & 255]);
}
const NS_HEX = '6f1c2b1e0b1a4c579d575a7a1c0a11aa';
function uuid5(name) {
  const ns = NS_HEX.match(/../g).map(h => parseInt(h, 16));
  const h = sha1(ns.concat(Array.from(new TextEncoder().encode(name)))).slice(0, 16);
  h[6] = (h[6] & 0x0f) | 0x50; h[8] = (h[8] & 0x3f) | 0x80;
  const x = h.map(b => b.toString(16).padStart(2, '0')).join('');
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

// tickets: [{id, subject, description, author:{name}, assigned_to?, category?, journals:[{user:{name}, notes}]}]
function buildRows(tickets, names, stop) {
  const clean = makeCleaner(names, stop);
  const rows = [];
  for (const t of tickets) {
    const author = (t.author || {}).name;
    const notes = (t.journals || []).filter(j => String(j.notes || '').trim() && (j.user || {}).name !== author)
      .map(j => clean(j.notes)).filter(n => cp(n).length >= 15);
    if (!notes.length) continue;
    const subject = clean(t.subject), description = cut(clean(t.description), 1500), answer = cut(notes.join('\n---\n'), 2000);
    const text = `Тема: ${subject}\nОпис: ${description}\nВідповідь інженера: ${answer}`;
    rows.push({ ticket: t.id, pid: uuid5(String(t.id)), text: cut(text, 3500), embed_text: cut(text, 3000),
      category: (t.category || {}).name || '', answer_len: Math.max(...notes.map(n => cp(n).length)) });
  }
  return rows;
}
"""

GLUE_JS = r"""
const cfg = $('Config').first().json;
const allIssues = $('Split Issues').all(), kept = $('Filter Recent').all();
if (allIssues.length >= Number(cfg.limit) && kept.length === allIssues.length) throw new Error(`Усі ${allIssues.length} останніх закритих заявок оновлені за ${cfg.days_back} дн.: ліміт замалий, частину могло бути пропущено`);
const tickets = $('[Redmine] Get Ticket').all().map(i => i.json.issue || i.json).filter(t => t && t.id);
const tab = $('[Data Table] Get Names').all().map(i => i.json);
const names = new Set(tab.filter(r => r.kind === 'name').map(r => String(r.word).toLowerCase()));
const stop = new Set(tab.filter(r => r.kind === 'stop').map(r => String(r.word).toLowerCase()));
// учасники цих заявок теж вважаються іменами (як у Python: автори, виконавці, автори коментарів)
for (const t of tickets) {
  const who = [(t.author || {}).name, (t.assigned_to || {}).name, ...(t.journals || []).map(j => (j.user || {}).name)];
  for (const n of who) for (const w of String(n || '').split(/[\s,]+/)) if (w.length >= 4 && /^\p{L}/u.test(w)) names.add(w.toLowerCase());
}
if (!names.size) throw new Error('таблиця kb_name_tokens порожня');
const rows = buildRows(tickets, names, stop).filter(r => r.answer_len >= Number(cfg.min_answer));
if (!rows.length) return [];
if (rows.length > 90) throw new Error(`Заявок до завантаження ${rows.length} > 90 (ліміт одного батчу Gemini)`);
const requests = rows.map(r => ({ model: 'models/gemini-embedding-001', content: { parts: [{ text: r.embed_text }] }, taskType: 'RETRIEVAL_DOCUMENT', title: 'ticket ' + r.ticket }));
return [{ json: { requests, rows } }];
"""

POINTS_JS = r"""
const prev = $('Code: Anonymize').first().json.rows;
const emb = $input.first().json.embeddings || [];
if (emb.length !== prev.length) throw new Error('Embeddings ' + emb.length + ' != rows ' + prev.length);
const points = prev.map((r, i) => ({ id: r.pid, vector: emb[i].values, payload: { content: r.text, metadata: { source: 'ticket', ticket: r.ticket, category: r.category, answer_len: r.answer_len } } }));
return [{ json: { points, tickets: prev.map(r => r.ticket) } }];
"""


def build(rm_id, rm_name, gem_id, gem_name, names_table, qdrant="http://<QDRANT_HOST>:6333", qdrant_cred=("<QDRANT_CRED_ID>", "<QDRANT_CRED_NAME>"),
          project_id="149"):
    nodes, conn = [], {}

    def node(name, type_, params, x, y, ver=1, **extra):
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        n.update(extra)
        nodes.append(n)

    tbl = {"__rl": True, "mode": "id", "value": names_table}
    rmc = {"redmineApi": {"id": rm_id, "name": rm_name}}
    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## KB: Weekly Refresh\n"
        "Щопонеділка 08:00: нові закриті заявки HelpDesk (за days_back днів) -> коментарі -> обезличення (Code: Anonymize, порт kb_anonymize.py) -> "
        "Gemini embedding -> Qdrant it_support_kb. Id точок = uuid5 від номера заявки: повтор не дублює. Плейбуки не чіпає.\n"
        "Слова для обезличення: таблиця kb_name_tokens (name / stop)."), "height": 190, "width": 640}, -200, -300)
    node("Every Monday 08:00", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "weeks", "weeksInterval": 1, "triggerAtDay": [1], "triggerAtHour": 8}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "days_back", "value": 10, "type": "number"},
        {"id": "a2", "name": "limit", "value": 100, "type": "number"},
        {"id": "a3", "name": "min_answer", "value": 80, "type": "number"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Redmine] Get Issues", "n8n-nodes-redmine.redmine", {"operation": "getAll", "returnAll": False, "limit": "={{ $json.limit }}",
         "filters": {"project_id": project_id, "status_id": "closed"}, "options": {}}, 440, 0, credentials=rmc, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
    node("Split Issues", "n8n-nodes-base.splitOut", {"fieldToSplitOut": "issues", "options": {}}, 660, 0, ver=1)
    node("Filter Recent", "n8n-nodes-base.filter", {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
         "conditions": [{"id": "c1", "leftValue": "={{ $json.updated_on }}", "rightValue": "={{ $now.minus({ days: $('Config').first().json.days_back }).toISO() }}",
                         "operator": {"type": "dateTime", "operation": "after"}}], "combinator": "and"}, "options": {}}, 770, 0, ver=2.2)
    node("[Redmine] Get Ticket", "n8n-nodes-redmine.redmine", {"operation": "get", "issueId": "={{ $json.id }}", "include": ["journals"], "options": {}},
         990, 0, credentials=rmc, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
    node("[Data Table] Get Names", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": tbl, "returnAll": True,
         "matchType": "anyCondition", "filters": {}}, 1210, 0, ver=1.1, alwaysOutputData=True, executeOnce=True)
    node("Code: Anonymize", "n8n-nodes-base.code", {"jsCode": (ANON_LIB + GLUE_JS).strip()}, 1430, 0, ver=2)
    node("[Gemini] Embed", "n8n-nodes-base.httpRequest", {
        "method": "POST", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "googlePalmApi", "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ requests: $json.requests }) }}", "options": {}}, 1650, 0, ver=4.2,
        credentials={"googlePalmApi": {"id": gem_id, "name": gem_name}}, retryOnFail=True, maxTries=3, waitBetweenTries=10000)
    node("Code: Build Points", "n8n-nodes-base.code", {"jsCode": POINTS_JS.strip()}, 1870, 0, ver=2)
    node("[Qdrant] Upsert", "n8n-nodes-base.httpRequest", {
        "method": "PUT", "url": f"{qdrant}/collections/it_support_kb/points?wait=true",
        "authentication": "predefinedCredentialType", "nodeCredentialType": "qdrantApi", "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ points: $json.points }) }}", "options": {}}, 2090, 0, ver=4.2,
        credentials={"qdrantApi": {"id": qdrant_cred[0], "name": qdrant_cred[1]}})
    chain = ["Every Monday 08:00", "Config", "[Redmine] Get Issues", "Split Issues", "Filter Recent", "[Redmine] Get Ticket", "[Data Table] Get Names",
             "Code: Anonymize", "[Gemini] Embed", "Code: Build Points", "[Qdrant] Upsert"]
    for a, b in zip(chain, chain[1:]):
        conn[a] = {"main": [[{"node": b, "type": "main", "index": 0}]]}
    return {"name": "Сервіс: KB Weekly Refresh", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1", "timezone": "Europe/Kyiv"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("redmine-cred", "<REDMINE_CRED>"), ("redmine-name", "Redmine <ENV> account"), ("gemini-cred", "<GEMINI_CRED>"),
                 ("gemini-name", "<GEMINI_NAME>"), ("names-table", "<NAME_TOKENS_TABLE>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "kb-weekly-refresh.json"))
    a = ap.parse_args()
    wf = build(a.redmine_cred, a.redmine_name, a.gemini_cred, a.gemini_name, a.names_table)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
