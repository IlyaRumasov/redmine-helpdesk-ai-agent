"""Збирає workflows/support-agent-offboarding-now.json (флоу «Автоматизатор: Offboarding Now»).

Кожні 5 хв дивиться заявки на звільнення (проєкт Starter|Leaver), змінені за останні хвилини. Якщо учасник «IT Адміни»
написав у заявці коментар «Обробити зараз», агент одразу викликає відповідний флоу оффбордингу
(спершу перевірка техніки в GLPI, потім лінійний — трекер 41, адмін — трекер 25) лише для цієї заявки, не чекаючи 18:00. Сам оффбординг робить усі свої перевірки
й пише звіт у заявку; команда записується в аудит, щоб не виконати її двічі.

Запуск: python tools/build_offboarding_now.py [--http-cred ID --http-name N --redmine-cred ID --redmine-name N --table-id ID
                                               --linear-id ID --admin-id ID]
"""
import argparse, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SPLIT_JS = r"""
// Відкриті заявки на звільнення, змінені нещодавно (може бути кілька сторінок, тут одна: за 15 хв їх небагато)
return (($input.first().json.issues) || []).map(i => ({ json: { id: i.id, tracker_id: i.tracker && i.tracker.id, subject: i.subject } }));
"""

FIND_JS = r"""
// Шукаємо свіжу команду «Обробити зараз» від учасника «IT Адміни». Кожну команду (id запису журналу) виконуємо один раз.
const cfg = $('Config').first().json;
const words = String(cfg.command_words).toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const admins = new Set((($('[Redmine] Get IT Admins').first().json.users) || []).map(u => String(u.id)));
const done = new Set($('[Data Table] Get Log').all().filter(r => r.json.kind === 'offboarding_command').map(r => String(r.json.payload)));
const since = Date.now() - Number(cfg.window_minutes) * 60000;
const tracker = { [String(cfg.linear_tracker_id)]: 'linear', [String(cfg.admin_tracker_id)]: 'admin' };
const out = [];
for (const { json: h } of $('[Redmine] Get Ticket History').all()) {
  const i = h.issue || h;
  const flow = tracker[String(i.tracker && i.tracker.id)];
  if (!flow) continue;
  const cmd = (i.journals || []).filter(j => {
    const n = String(j.notes || '').trim().toLowerCase().replace(/[.!\s]+$/, '');
    return words.includes(n) && Date.parse(j.created_on) >= since && admins.has(String(j.user && j.user.id));
  }).pop();
  if (!cmd || done.has(String(cmd.id))) continue;
  out.push({ json: { ticket_id: i.id, flow, journal_id: cmd.id, by: cmd.user.name, at: new Date().toISOString() } });
}
return out;
"""


def build(http_id, http_name, rm_id, rm_name, table_id, linear_id, admin_id, equipment_id):
    nodes, conn = [], {}

    def node(name, type_, params, x, y, ver=1, **extra):
        n = {"parameters": params, "type": type_, "typeVersion": ver, "position": [x, y], "id": str(uuid.uuid4()), "name": name}
        n.update(extra)
        nodes.append(n)

    def link(a, b, out=0):
        m = conn.setdefault(a, {"main": []})["main"]
        while len(m) <= out:
            m.append([])
        m[out].append({"node": b, "type": "main", "index": 0})

    cfgv = lambda k: "={{ $('Config').first().json.%s }}" % k
    table = {"__rl": True, "mode": "id", "value": table_id}
    run_flow = lambda name, key, y: node(name, "n8n-nodes-base.executeWorkflow", {
        "source": "database", "workflowId": {"__rl": True, "mode": "id", "value": cfgv(key)}, "mode": "each",
        "options": {"waitForSubWorkflow": True}}, 1980, y, ver=1.2, onError="continueRegularOutput")

    node("Sticky Note", "n8n-nodes-base.stickyNote", {"content": (
        "## Автоматизатор: Offboarding Now\n"
        "Кожні 5 хв: заявки на звільнення, змінені за останні 15 хв. Коментар «Обробити зараз» від учасника «IT Адміни»\n"
        "-> спершу перевірка техніки в GLPI, потім флоу оффбордингу (трекер 41 лінійний, 25 адмін) лише для цієї заявки.\n"
        "Оффбординг сам перевіряє дату, AD, Google і пише звіт. Команда пишеться в аудит і виконується один раз."), "height": 190, "width": 620}, -200, -300)
    node("Every 5 Min", "n8n-nodes-base.scheduleTrigger", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, 0, 0, ver=1.2)
    node("Config", "n8n-nodes-base.set", {"assignments": {"assignments": [
        {"id": "a1", "name": "redmine_url", "value": "<REDMINE_URL>", "type": "string"},
        {"id": "a2", "name": "project_id", "value": "<LEAVER_PROJECT_ID>", "type": "string"},
        {"id": "a3", "name": "linear_tracker_id", "value": "41", "type": "string"},
        {"id": "a4", "name": "admin_tracker_id", "value": "25", "type": "string"},
        {"id": "a5", "name": "linear_flow_id", "value": linear_id, "type": "string"},
        {"id": "a6", "name": "admin_flow_id", "value": admin_id, "type": "string"},
        {"id": "a10", "name": "equipment_flow_id", "value": equipment_id, "type": "string"},
        {"id": "a7", "name": "admins_group_id", "value": "35", "type": "string"},
        {"id": "a8", "name": "command_words", "value": "обробити зараз,обработать сейчас", "type": "string"},
        {"id": "a9", "name": "window_minutes", "value": 15, "type": "number"}]}, "options": {}}, 220, 0, ver=3.4)
    node("[Data Table] Get Log", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "get", "dataTableId": table,
         "returnAll": True, "matchType": "anyCondition", "filters": {}}, 440, 0, ver=1.1, alwaysOutputData=True)
    node("[Redmine] Get Recently Updated", "n8n-nodes-base.httpRequest", {
        "url": "={{ $('Config').first().json.redmine_url }}/issues.json?project_id={{ $('Config').first().json.project_id }}&status_id=open&limit=100"
               "&updated_on=%3E%3D{{ encodeURIComponent(new Date(Date.now() - $('Config').first().json.window_minutes * 60000).toISOString().slice(0, 19) + 'Z') }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth", "options": {}},
         660, 0, ver=4.2, credentials={"httpHeaderAuth": {"id": http_id, "name": http_name}}, executeOnce=True)
    node("Code: Split Issues", "n8n-nodes-base.code", {"jsCode": SPLIT_JS.strip()}, 880, 0, ver=2)
    node("[Redmine] Get Ticket History", "n8n-nodes-redmine.redmine", {"operation": "get", "issueId": "={{ $json.id }}", "include": ["journals"],
         "options": {}}, 1100, 0, credentials={"redmineApi": {"id": rm_id, "name": rm_name}})
    node("[Redmine] Get IT Admins", "n8n-nodes-redmine.redmine", {"resource": "user", "operation": "getAll", "returnAll": True,
         "filters": {"group_id": cfgv("admins_group_id"), "status": "1"}, "options": {}}, 1320, 0,
         credentials={"redmineApi": {"id": rm_id, "name": rm_name}}, executeOnce=True, alwaysOutputData=True)
    node("Code: Find Commands", "n8n-nodes-base.code", {"jsCode": FIND_JS.strip()}, 1540, 0, ver=2)
    node("Route by Flow", "n8n-nodes-base.switch", {"rules": {"values": [
        {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                        "conditions": [{"id": "r" + f, "leftValue": "={{ $json.flow }}", "rightValue": f, "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"}, "renameOutput": True, "outputKey": f} for f in ("linear", "admin")]}, "options": {}}, 1760, 0, ver=3.2)
    # спершу перевірка техніки (оффбординг закриває заявку, лише бачачи свіжий висновок про техніку), потім оффбординг
    run_flow("Run: Equipment Check", "equipment_flow_id", -360)
    run_flow("Run: Linear Offboarding", "linear_flow_id", -120)
    run_flow("Run: Admin Offboarding", "admin_flow_id", 120)
    cols = [("ticket_id", "number"), ("kind", "string"), ("outcome", "string"), ("dry_run", "boolean"), ("details", "string"), ("at", "string"),
            ("payload", "string")]
    src = "Code: Find Commands"
    vals = {"ticket_id": "={{ $('%s').item.json.ticket_id }}" % src, "kind": "offboarding_command",
            "outcome": "={{ $json.error ? 'ERROR: ' + ($json.error.message || $json.error) : 'started' }}", "dry_run": "={{ false }}",
            "details": "={{ 'флоу ' + $('%s').item.json.flow + ', команду дав ' + $('%s').item.json.by }}" % (src, src),
            "at": "={{ $('%s').item.json.at }}" % src, "payload": "={{ String($('%s').item.json.journal_id) }}" % src}
    node("[Data Table] Log Command", "n8n-nodes-base.dataTable", {"resource": "row", "operation": "insert", "dataTableId": table, "columns": {
        "mappingMode": "defineBelow", "matchingColumns": [], "value": vals,
        "schema": [{"id": c, "displayName": c, "required": False, "defaultMatch": False, "display": True, "type": t, "canBeUsedToMatch": True} for c, t in cols]},
        "options": {}}, 2200, 0, ver=1.1)
    for a, b in [("Every 5 Min", "Config"), ("Config", "[Data Table] Get Log"), ("[Data Table] Get Log", "[Redmine] Get Recently Updated"),
                 ("[Redmine] Get Recently Updated", "Code: Split Issues"), ("Code: Split Issues", "[Redmine] Get Ticket History"),
                 ("[Redmine] Get Ticket History", "[Redmine] Get IT Admins"), ("[Redmine] Get IT Admins", "Code: Find Commands"),
                 ("Run: Linear Offboarding", "[Data Table] Log Command"),
                 ("Run: Admin Offboarding", "[Data Table] Log Command")]:
        link(a, b)
    # гілка перевірки техніки перша: у порядку v1 вона виконується повністю до маршрутизації на оффбординг
    link("Code: Find Commands", "Run: Equipment Check")
    link("Code: Find Commands", "Route by Flow")
    link("Route by Flow", "Run: Linear Offboarding", 0)
    link("Route by Flow", "Run: Admin Offboarding", 1)
    return {"name": "Автоматизатор: Offboarding Now", "nodes": nodes, "connections": conn, "settings": {"executionOrder": "v1"}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k, d in [("http-cred", "<REDMINE_HTTP_CRED>"), ("http-name", "Redmine HTTP"), ("redmine-cred", "<REDMINE_CRED>"),
                 ("redmine-name", "Redmine <ENV> account"), ("table-id", "<AUDIT_TABLE>"), ("linear-id", "<LINEAR_OFFBOARDING_ID>"),
                 ("admin-id", "<ADMIN_OFFBOARDING_ID>"), ("equipment-id", "<EQUIPMENT_CHECK_ID>")]:
        ap.add_argument("--" + k, default=d)
    ap.add_argument("--out", default=str(ROOT / "workflows" / "support-agent-offboarding-now.json"))
    a = ap.parse_args()
    wf = build(a.http_cred, a.http_name, a.redmine_cred, a.redmine_name, a.table_id, a.linear_id, a.admin_id, a.equipment_id)
    Path(a.out).write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    print("nodes:", len(wf["nodes"]), "->", a.out)
