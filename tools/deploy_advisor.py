"""Завантажує флоу радника (6 інструментів + агент) у живий n8n ВИМКНЕНИМИ, з бойовими кредами, DRY_RUN=true.

python tools/deploy_advisor.py   створити або оновити за назвою; активні флоу не чіпає (спершу вимкнути)
Агент у режимі DRY_RUN пише нотатку лише в аудит. Вмикати (інструменти, потім агента) лише після явного «так».
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_tmp import call
from settings import S
import build_advisor_tools as BT
import build_advisor_agent as BA

COORDINATORS = (Path(__file__).resolve().parent / "coordinators.txt").read_text().strip()
PROJECT = "149"


def patch(wf, cfg):
    for n in wf["nodes"]:
        if n["name"] == "Config":
            for a in n["parameters"]["assignments"]["assignments"]:
                if a["name"] in cfg:
                    a["value"] = cfg[a["name"]]
    return wf


def upsert(wf, existing):
    body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
    if S.get("error_workflow_id"):  # помилки будь-якого флоу йдуть у [System] Global Error Handler (заявка в Redmine)
        body["settings"] = {**body["settings"], "errorWorkflow": S["error_workflow_id"]}
    w = existing.get(wf["name"])
    if w:
        if w.get("active"):
            print("ПРОПУЩЕНО (активний, спершу вимкнути):", wf["name"])
            return w["id"]
        r = call("PUT", f"/api/v1/workflows/{w['id']}", body)
        print("оновлено ", r["id"], wf["name"], "active:", r.get("active"))
    else:
        r = call("POST", "/api/v1/workflows", body)
        print("створено ", r["id"], wf["name"], "active:", r.get("active"))
    return r["id"]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    existing = {w["name"]: w for w in call("GET", "/api/v1/workflows?limit=250")["data"]}
    rm, ld, gg, gem, gl = S["prod_redmine_cred"], S["ldap_cred"], S["google_cred"], S["gemini_cred"], S["glpi_cred"]
    tools = BT.build_all({"rm": (rm["id"], rm["name"]), "gem": gem, "ldap": (ld["id"], ld["name"]), "google": (gg["id"], gg["name"]),
                          "glpi": (gl["id"], gl["name"], S["glpi_url"])})
    patch(tools["advisor-ticket"], {"project_id": PROJECT})
    ids = {}
    for key, name in [("ticket", "advisor-ticket"), ("similar", "advisor-similar"), ("policies", "advisor-policies"), ("ad", "advisor-ad-user"),
                      ("google", "advisor-google-guest"), ("glpi", "advisor-glpi")]:
        ids[key] = upsert(tools[name], existing)
    agent = BA.build(rm["id"], rm["name"], gem["id"], gem["name"], S["prod_table"], ids)
    patch(agent, {"dry_run": True, "project_id": PROJECT, "allowed_author_ids": COORDINATORS, "max_per_run": "1"})
    upsert(agent, existing)


if __name__ == "__main__":
    main()
