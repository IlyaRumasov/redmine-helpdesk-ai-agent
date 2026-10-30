"""Перетворює флоу-інструмент на під-воркфлоу для диспетчера.

Замість «Every 5 Min» і «[Redmine] Get New Tickets» з'являються «When Called by Dispatcher»
(вхід: {issues: [...]}, у заявок може бути поле ai з підказкою) і «Take Dispatched Tickets».
Решта флоу (розбір, перевірки, дії, звіт, аудит) не змінюється.
"""
import copy, uuid

TRIGGER = "When Called by Dispatcher"
TAKE = "Take Dispatched Tickets"


def to_subworkflow(wf):
    wf = copy.deepcopy(wf)
    nodes = [n for n in wf["nodes"] if n["name"] not in ("Every 5 Min", "[Redmine] Get New Tickets")]
    nodes.append({"parameters": {"inputSource": "passthrough"}, "type": "n8n-nodes-base.executeWorkflowTrigger", "typeVersion": 1.1,
                  "position": [0, 0], "id": str(uuid.uuid4()), "name": TRIGGER})
    nodes.append({"parameters": {"assignments": {"assignments": [
        {"id": "t1", "name": "issues", "value": "={{ $('%s').first().json.issues || [] }}" % TRIGGER, "type": "array"}]}, "options": {}},
        "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [660, 160], "id": str(uuid.uuid4()), "name": TAKE})
    conn = {}
    for src, v in wf["connections"].items():
        if src in ("Every 5 Min", "[Redmine] Get New Tickets"):
            continue
        outs = []
        for branch in v["main"]:
            outs.append([dict(c, node=TAKE) if c["node"] == "[Redmine] Get New Tickets" else c for c in branch])
        conn[src] = {"main": outs}
    conn[TRIGGER] = {"main": [[{"node": "Config", "type": "main", "index": 0}]]}
    conn[TAKE] = {"main": [[{"node": "Code: Parse Tickets", "type": "main", "index": 0}]]}
    wf["nodes"], wf["connections"] = nodes, conn
    wf["name"] = wf["name"] + " (tool)"
    return wf
