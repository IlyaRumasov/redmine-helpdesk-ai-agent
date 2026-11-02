"""Завантажує обезличені рішення заявок у колекцію it_support_kb (Qdrant) через тимчасові флоу n8n.

Ключі Gemini і Qdrant лишаються в креденшлах n8n (їх тут немає). Вхід: redmine-analysis/kb/kb_tickets.jsonl (після kb_anonymize.py).
Колекція: 3072, Cosine (як policies_kb). Айді точок детерміновані (uuid5 від номера заявки), тож повторний запуск перезаписує, а не дублює.

python tools/kb_load.py [--min-answer 80] [--batch 90] [--dry]
"""
import argparse, json, sys, uuid
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
                "const points = prev.map((r, i) => ({ id: r.pid, vector: emb[i].values, payload: { content: r.text, metadata: { source: 'ticket', ticket: r.ticket, category: r.category, answer_len: r.answer_len } } }));\n"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-answer", type=int, default=80)
    ap.add_argument("--batch", type=int, default=90)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--collection", default=COLL)
    ap.add_argument("--exclude", help="JSON-файл зі списком id заявок, яких не завантажувати (контрольна вибірка для оцінки)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    rows = [json.loads(l) for l in KB.read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r["answer_len"] >= a.min_answer]
    if a.exclude:
        skip = {str(x["id"] if isinstance(x, dict) else x) for x in json.load(open(a.exclude, encoding="utf-8"))}
        rows = [r for r in rows if str(r["id"]) not in skip]
    items = []
    for r in rows:
        text = f"Тема: {r['subject']}\nОпис: {r['description']}\nВідповідь інженера: {r['answer']}"
        items.append({"ticket": r["id"], "pid": str(uuid.uuid5(NS, str(r["id"]))), "text": text[:3500], "embed_text": text[:3000],
                      "category": r.get("category") or "", "answer_len": r["answer_len"]})
    print("до завантаження:", len(items), "заявок; символів:", sum(len(i["text"]) for i in items))
    if a.dry:
        return
    ensure_collection(a.collection)
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
