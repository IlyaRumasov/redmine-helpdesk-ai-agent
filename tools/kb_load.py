"""Завантажує обезличені рішення заявок у колекцію it_support_kb (Qdrant) через тимчасові флоу n8n.

Ключі Gemini і Qdrant лишаються в креденшлах n8n (їх тут немає). Вхід: redmine-analysis/kb/kb_tickets.jsonl (після kb_anonymize.py).
Колекція: 3072, Cosine (як policies_kb). Айді точок детерміновані (uuid5 від номера заявки), тож повторний запуск перезаписує, а не дублює.

python tools/kb_load.py [--min-answer 80] [--batch 90] [--dry]
python tools/kb_load.py --playbooks <файл.md> [--only 1,2,5] [--dry]   затверджені плейбуки (source=playbook, шматки до 880 символів)
"""
import argparse, json, re, sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_tmp import run_temp, outputs
from settings import S

QDRANT = "http://<QDRANT_HOST>:6333"
COLL = "it_support_kb"
QCRED = {"qdrantApi": {"id": "<QDRANT_CRED_ID>", "name": "<QDRANT_CRED_NAME>"}}
KB = Path(r"C:\Claude\redmine-analysis\kb\kb_tickets.jsonl")
NS = uuid.UUID("6f1c2b1e-0b1a-4c57-9d57-5a7a1c0a11aa")


def qhttp(name, method, url, body=None, x=300):
    p = {"method": method, "url": url, "authentication": "predefinedCredentialType", "nodeCredentialType": "qdrantApi", "options": {}}
    if body is not None:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(body, ensure_ascii=False)})
    return {"parameters": p, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [x, 0], "id": str(uuid.uuid4()), "name": name, "credentials": QCRED}


def ensure_collection(coll=COLL):
    nodes = [qhttp("Get", "GET", f"{QDRANT}/collections/{coll}", None, 300)]
    nodes[0]["onError"] = "continueRegularOutput"
    nodes.append(qhttp("Create", "PUT", f"{QDRANT}/collections/{coll}", {"vectors": {"size": 3072, "distance": "Cosine"}}, 500))
    nodes[1]["onError"] = "continueRegularOutput"
    # створюємо лише якщо читання дало помилку (колекції немає): через IF
    iff = {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                                         "conditions": [{"id": "c", "leftValue": "={{ $json.result ? 'yes' : 'no' }}", "rightValue": "no", "operator": {"type": "string", "operation": "equals"}}],
                                         "combinator": "and"}, "options": {}},
           "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [400, 0], "id": str(uuid.uuid4()), "name": "Missing?"}
    conn = {"Webhook": {"main": [[{"node": "Get", "type": "main", "index": 0}]]}, "Get": {"main": [[{"node": "Missing?", "type": "main", "index": 0}]]},
            "Missing?": {"main": [[{"node": "Create", "type": "main", "index": 0}], []]}}
    rd = run_temp("[TEMP] kb collection", [nodes[0], iff, nodes[1]], conn, wait=60)
    g = outputs(rd, "Get")
    print("колекція вже була:", bool(g and g[0].get("result")), "| створено:", "Create" in rd["runData"])


def batch_flow(rows, coll=COLL):
    emb_code = ("const rows = $input.first().json.body.rows;\n"
                "const requests = rows.map(r => ({ model: 'models/gemini-embedding-001', content: { parts: [{ text: r.embed_text }] }, taskType: 'RETRIEVAL_DOCUMENT', title: 'ticket ' + r.ticket }));\n"
                "return [{ json: { requests, rows } }];")
    pts_code = ("const prev = $('Build Requests').first().json.rows;\n"
                "const emb = $input.first().json.embeddings || [];\n"
                "if (emb.length !== prev.length) throw new Error('Embeddings ' + emb.length + ' != rows ' + prev.length);\n"
                "const points = prev.map((r, i) => ({ id: r.pid, vector: emb[i].values, payload: { content: r.text, metadata: { source: r.source || 'ticket', ticket: r.ticket, category: r.category, answer_len: r.answer_len } } }));\n"
                "return [{ json: { points } }];")
    code = lambda name, js, x: {"parameters": {"jsCode": js}, "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [x, 0], "id": str(uuid.uuid4()), "name": name}
    gem = {"parameters": {"method": "POST", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents",
                          "authentication": "predefinedCredentialType", "nodeCredentialType": "googlePalmApi", "sendBody": True, "specifyBody": "json",
                          "jsonBody": "={{ JSON.stringify({ requests: $json.requests }) }}", "options": {}},
           "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [700, 0], "id": str(uuid.uuid4()), "name": "Embed",
           "credentials": {"googlePalmApi": dict(S["gemini_cred"])}}
    up = qhttp("Upsert", "PUT", f"{QDRANT}/collections/{coll}/points?wait=true", None, 1100)
    up["parameters"].update({"sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ points: $json.points }) }}"})
    nodes = [code("Build Requests", emb_code, 400), gem, code("Build Points", pts_code, 900), up]
    names = ["Webhook", "Build Requests", "Embed", "Build Points", "Upsert"]
    conn = {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(names, names[1:])}
    return nodes, conn


CHUNK = 880  # інструмент advisor_similar віддає лише перші 900 символів уривка, тож шматок плейбука не довший
SKIP_SECTIONS = ("питання до інженерів", "джерела")  # службові блоки для перевірки інженерами, у базу не йдуть


def playbook_items(path, only=None):
    """Затверджені плейбуки (markdown: «## Плейбук N. Назва», далі текст) -> точки для it_support_kb.

    Кожен плейбук ріжеться за абзацами на шматки до CHUNK символів з назвою на початку; ticket = playbook-N, category = playbook.
    """
    items, cur, n = [], None, None
    parts = re.split(r"(?m)^##\s+", Path(path).read_text(encoding="utf-8"))
    for part in parts[1:]:
        head, _, body = part.partition("\n")
        m = re.match(r"(?:Плейбук\s+)?(\d+)[.:)]?\s*(.*)", head.strip(), re.I)
        if not m or (only and int(m.group(1)) not in only):
            continue
        n, title = int(m.group(1)), m.group(2).strip()
        paras, skip = [], False
        for p in re.split(r"\n\s*\n", body.strip()):
            low = p.strip().lower().lstrip("*# ")
            if low.startswith(SKIP_SECTIONS):
                skip = True
            elif re.match(r"\*\*[^*]+:\*\*", p.strip()):
                skip = False
            if p.strip() and not skip:
                paras.append(p.strip())
        chunks, cur = [], ""
        prefix = f"Плейбук {n}: {title}\n"
        for p in paras:
            if cur and len(prefix + cur + "\n\n" + p) > CHUNK:
                chunks.append(cur); cur = p
            else:
                cur = (cur + "\n\n" + p) if cur else p
        if cur: chunks.append(cur)
        for i, c in enumerate(chunks):
            text = (prefix + c)[:3500]
            items.append({"ticket": f"playbook-{n}", "pid": str(uuid.uuid5(NS, f"playbook:{n}:{i}")), "text": text, "embed_text": text,
                          "category": "playbook", "answer_len": len(c), "source": "playbook"})
    return items


def delete_playbooks(coll):
    """Перед повторним завантаженням прибирає старі шматки плейбуків (щоб не лишалися сироти, якщо шматків стало менше)."""
    body = {"filter": {"must": [{"key": "metadata.source", "match": {"value": "playbook"}}]}}
    rd = run_temp("[TEMP] kb playbooks delete", [qhttp("Delete", "POST", f"{QDRANT}/collections/{coll}/points/delete?wait=true", body, 400)],
                  {"Webhook": {"main": [[{"node": "Delete", "type": "main", "index": 0}]]}}, wait=60)
    print("старі шматки плейбуків прибрано:", (outputs(rd, "Delete") or [{}])[0].get("status"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-answer", type=int, default=80)
    ap.add_argument("--batch", type=int, default=90)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--collection", default=COLL)
    ap.add_argument("--exclude", help="JSON-файл зі списком id заявок, яких не завантажувати (контрольна вибірка для оцінки)")
    ap.add_argument("--ids", help="JSON-файл зі списком id заявок: завантажити лише їх (щотижневе оновлення)")
    ap.add_argument("--playbooks", help="markdown із затвердженими плейбуками замість заявок; старі шматки плейбуків у колекції замінюються")
    ap.add_argument("--append", action="store_true", help="з --playbooks: не видаляти старі плейбуки, лише додати/оновити вказані (за стабільними id)")
    ap.add_argument("--only",help="номери плейбуків через кому (лише затверджені), напр. 1,2,5")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.playbooks:
        items = playbook_items(a.playbooks, {int(x) for x in a.only.split(",")} if a.only else None)
        print("до завантаження:", len(items), "шматків плейбуків:", sorted({i["ticket"] for i in items}), "символів:", sum(len(i["text"]) for i in items))
    else:
        rows = [json.loads(l) for l in KB.read_text(encoding="utf-8").splitlines()]
        rows = [r for r in rows if r["answer_len"] >= a.min_answer]
        if a.exclude:
            skip = {str(x["id"] if isinstance(x, dict) else x) for x in json.load(open(a.exclude, encoding="utf-8"))}
            rows = [r for r in rows if str(r["id"]) not in skip]
        if a.ids:
            only = {str(x) for x in json.load(open(a.ids, encoding="utf-8"))}
            rows = [r for r in rows if str(r["id"]) in only]
        items = []
        for r in rows:
            text = f"Тема: {r['subject']}\nОпис: {r['description']}\nВідповідь інженера: {r['answer']}"
            items.append({"ticket": r["id"], "pid": str(uuid.uuid5(NS, str(r["id"]))), "text": text[:3500], "embed_text": text[:3000],
                          "category": r.get("category") or "", "answer_len": r["answer_len"]})
        print("до завантаження:", len(items), "заявок; символів:", sum(len(i["text"]) for i in items))
    if a.dry:
        return
    ensure_collection(a.collection)
    if a.playbooks and not a.append:
        delete_playbooks(a.collection)
    done = 0
    for s in range(0, len(items), a.batch):
        part = items[s:s + a.batch]
        nodes, conn = batch_flow(part, a.collection)
        rd = run_temp(f"[TEMP] kb load {s}", nodes, conn, payload={"rows": part}, wait=240)
        err = (rd.get("error") or {}).get("message")
        up = outputs(rd, "Upsert")
        ok = bool(up) and "__error" not in up[0] and (up[0].get("result") or {}).get("status") in ("completed", "acknowledged")
        print(f"партія {s}-{s + len(part)}: {'ок' if ok else 'ПОМИЛКА ' + str(err or up[:1])}")
        done += len(part) if ok else 0
    print("завантажено:", done, "із", len(items))


if __name__ == "__main__":
    main()
