"""Показує, яку нотатку написав би радник на конкретну заявку. Нічого не пише в Redmine і не чіпає аудит.

Використовує ті самі тимчасові інструменти й схему, що eval_advisor.py, але з бойовою базою знань it_support_kb.
python tools/sim_advisor_ticket.py <файл.json з полями id, subject, description>
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_tmp import call, run_temp, outputs
from settings import S
import build_advisor_tools as BT
import eval_advisor as EA


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    t = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cfg = {"rm": (S["prod_redmine_cred"]["id"], S["prod_redmine_cred"]["name"]), "gem": S["gemini_cred"], "ldap": (S["ldap_cred"]["id"], S["ldap_cred"]["name"]),
           "google": (S["google_cred"]["id"], S["google_cred"]["name"]), "glpi": (S["glpi_cred"]["id"], S["glpi_cred"]["name"], S["glpi_url"])}
    tools = BT.build_all(cfg)
    created = []
    try:
        ids = {}
        for key, name in [("ticket", "advisor-ticket"), ("similar", "advisor-similar"), ("policies", "advisor-policies"), ("ad", "advisor-ad-user"),
                          ("google", "advisor-google-guest"), ("glpi", "advisor-glpi")]:
            ids[key] = EA.make_tool(tools[name]); created.append(ids[key])
        nodes, conn = EA.replay_flow(ids)
        rd = run_temp(f"[TEMP-SIM] advisor {t['id']}", nodes, conn, payload=t, wait=300)
        o = outputs(rd, "Code: Build Note")
        res = o[0] if o else {"outcome": "error", "details": (rd.get("error") or {}).get("message")}
        print("outcome:", res.get("outcome"), "| інструменти:", res.get("tools"))
        print("\n--- НОТАТКА ---\n" + (res.get("note") or res.get("details") or ""))
    finally:
        for w in created:
            try: call("POST", f"/api/v1/workflows/{w}/deactivate", {})
            except Exception: pass
            call("DELETE", f"/api/v1/workflows/{w}")
        print("\nтимчасові інструменти видалено:", len(created))


if __name__ == "__main__":
    main()
