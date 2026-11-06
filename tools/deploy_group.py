"""Створює (або оновлює за назвою) два флоу груп Redmine у живому n8n ВИМКНЕНИМИ, з бойовими кредами і DRY_RUN=true.

python tools/deploy_group.py
Активний флоу не чіпає. Вмикати лише після явного «так» користувача.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from n8n_tmp import call
import build_group, build_group_apply
from deploy import configure, upsert
from settings import S

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    existing = {w["name"]: w for w in call("GET", "/api/v1/workflows?limit=250")["data"]}
    args = (*S["prod_redmine_cred"].values(), *S["prod_redmine_http_cred"].values(), S["prod_table"])
    for mod in (build_group, build_group_apply):
        wf = configure(mod.build(*args))
        for n in wf["nodes"]:
            if n["name"] == "Config":
                for a in n["parameters"]["assignments"]["assignments"]:
                    a["value"] = {"redmine_url": S["prod_redmine_url"].rstrip("/")}.get(a["name"], a["value"])
        upsert(wf, existing)
