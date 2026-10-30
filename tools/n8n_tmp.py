"""Спільні функції: тимчасовий флоу n8n з вебхуком, який сам видаляється."""
import json, time, uuid, urllib.request
from settings import S
B = S['n8n_url']
KEY = open(S['n8n_key_file']).read().strip()
H = {"X-N8N-API-KEY": KEY, "Content-Type": "application/json", "User-Agent": "curl/8.7.1"}

def call(m, p, body=None, raw=False):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode() if body is not None else None, method=m, headers=H)
    t = urllib.request.urlopen(r, timeout=180).read().decode()
    return t if raw else (json.loads(t) if t else {})

def run_temp(name, nodes, conn, trigger_name="Webhook", payload=None, wait=120):
    path = 'tmp-' + uuid.uuid4().hex[:10]
    nodes = [{"parameters": {"httpMethod": "POST", "path": path, "options": {}}, "type": "n8n-nodes-base.webhook", "typeVersion": 2,
              "position": [0, 0], "id": str(uuid.uuid4()), "name": trigger_name, "webhookId": str(uuid.uuid4())}] + nodes
    wid = call("POST", "/api/v1/workflows", {"name": name, "settings": {"executionOrder": "v1"}, "nodes": nodes, "connections": conn})["id"]
    try:
        call("POST", f"/api/v1/workflows/{wid}/activate", {}); time.sleep(2)
        call("POST", f"/webhook/{path}", payload or {}, raw=True)
        d = {}
        for _ in range(wait):
            time.sleep(1)
            d = (call("GET", f"/api/v1/executions?workflowId={wid}&limit=1").get("data") or [{}])[0]
            if d.get("id") and d.get("status") not in ("running", "new", "waiting"):
                break
        return call("GET", f"/api/v1/executions/{d['id']}?includeData=true")["data"]["resultData"]
    finally:
        try: call("POST", f"/api/v1/workflows/{wid}/deactivate", {})
        except Exception: pass
        call("DELETE", f"/api/v1/workflows/{wid}")

def outputs(rd, node):
    run = rd["runData"].get(node, [{}])[0]
    if run.get("error"): return [{"__error": run["error"].get("message")}]
    return [i["json"] for br in (run.get("data", {}).get("main") or []) for i in (br or []) if i and "json" in i]
