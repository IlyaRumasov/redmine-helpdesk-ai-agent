"""Завантажує три флоу агента в живий n8n ВИМКНЕНИМИ, з бойовими кредами, DRY_RUN=true і порожнім списком координаторів.

python tools/deploy.py            - створити (або оновити за назвою) і вивести id
Флоу не активуються: вмикати лише після явного «так» користувача.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_tmp import call
import build_unlock, build_ad, build_apply, build_guest, build_guest_apply, build_dispatcher, build_offboarding_now
from subflow import to_subworkflow

ROOT = Path(__file__).resolve().parent.parent
from settings import S
RM = tuple(S["prod_redmine_cred"].values())
LDAP = tuple(S["ldap_cred"].values())
TABLE = S["prod_table"]  # бойова таблиця аудиту
COORDINATORS = (Path(__file__).resolve().parent / "coordinators.txt").read_text().strip()  # id координаторів, не комітити
CONFIG = {"dry_run": True, "project_id": "<PROJECT_ID>", "tracker_id": "<TRACKER_ID>", "allowed_author_ids": COORDINATORS, "mail_from": S.get("mail_from", "<MAIL_FROM>")}
PREFIX = "[PILOT] "


def configure(wf):
    for n in wf["nodes"]:
        if n["name"] == "Config":
            for a in n["parameters"]["assignments"]["assignments"]:
                if a["name"] in CONFIG:
                    a["value"] = CONFIG[a["name"]]
    wf["name"] = PREFIX + wf["name"]
    return wf


def drop_missing_smtp(wf):
    """Поки службової скриньки немає, вузол листа лишається без креду (флоу все одно вимкнений)."""
    for n in wf["nodes"]:
        if n["type"] == "n8n-nodes-base.emailSend" and not n["credentials"]["smtp"]["id"]:
            n.pop("credentials")
    return wf


def upsert(wf, existing):
    body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
    if S.get("error_workflow_id"):  # помилки будь-якого флоу йдуть у [System] Global Error Handler (заявка в Redmine)
        body["settings"] = {**body["settings"], "errorWorkflow": S["error_workflow_id"]}
    if wf["name"] in existing:
        w = existing[wf["name"]]
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
    smtp = S.get("smtp_cred", {"id": "", "name": ""})
    unlock = build_unlock.build(*RM, TABLE)
    ad = build_ad.build(*RM, *LDAP, TABLE)
    guest = drop_missing_smtp(build_guest.build(*RM, *LDAP, *S["google_cred"].values(), *smtp.values(), TABLE))
    # окремі флоу за розкладом (як раніше) і флоу підтверджених змін
    guest_apply = drop_missing_smtp(build_guest_apply.build(*RM, *S["google_cred"].values(), *smtp.values(), TABLE))
    for wf in (unlock, ad, build_apply.build(*RM, *LDAP, TABLE), guest, guest_apply):
        upsert(configure(json.loads(json.dumps(wf))), existing)
    # інструменти для диспетчера і сам диспетчер (усе вимкнене, DRY RUN в інструментах)
    tools = {k: upsert(configure(to_subworkflow(wf)), existing) for k, wf in (("unlock", unlock), ("ad", ad), ("guest", guest))}
    disp = build_dispatcher.build(*RM, *S["gemini_cred"].values(), TABLE, tools["unlock"], tools["ad"], tools["guest"])
    upsert(configure(disp), existing)
    # запуск оффбордингу за командою «Обробити зараз» (вимкнений)
    now = build_offboarding_now.build(*S["prod_redmine_http_cred"].values(), *RM, TABLE, S["linear_offboarding_id"], S["admin_offboarding_id"], S["equipment_check_id"])
    for n in now["nodes"]:
        if n["name"] == "Config":
            for a in n["parameters"]["assignments"]["assignments"]:
                a["value"] = {"redmine_url": S["prod_redmine_url"], "project_id": S["prod_leaver_project"]}.get(a["name"], a["value"])
    now["name"] = PREFIX + now["name"]
    upsert(now, existing)


if __name__ == "__main__":
    main()
