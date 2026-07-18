#!/usr/bin/env python3
"""Cloud Dream Engine (Phase 16 port of scripts/dream-engine.sh).

The local Dream Engine mined this host's session logs (unavailable from a cloud
runner). The cloud incarnation mines the SHARED cloud memory (D1: recent agent
runs + directives) and asks cloud-brain-proxy to surface one repeated pattern
worth a reusable skill. If it produces one, it is written to shared memory
(directive section 'dream:latest-skill') so it can be reviewed/adopted. No local
filesystem, no systemd — purely cloud-native.

Env: BRAIN_URL / PROXY_AUTH_TOKEN (from asi_cloud)
"""
import json
import time

import asi_cloud as asi

SYSTEM = (
    "You are a skill-synthesis agent. Given recent autonomous-agent activity, "
    "identify AT MOST ONE recurring workflow or gap worth turning into a reusable "
    "skill. Respond as compact JSON: "
    '{\"skill_name\": str, \"trigger\": str, \"why\": str, \"steps\": [str], '
    '\"worth_it\": bool}. If nothing recurs, set worth_it=false.'
)


def gather_context():
    rows = []
    try:
        rows = asi.d1_query(
            "SELECT section, substr(content,1,600) AS content, updated_at "
            "FROM directives WHERE section LIKE 'cloud-agent:%' "
            "ORDER BY updated_at DESC LIMIT 20")
    except asi.ASIError as e:
        print("[warn] context query failed:", e)
    lines = [f"- {r['section']}: {r['content']}" for r in rows]
    return "\n".join(lines) or "(no recent agent activity)"


def main():
    ctx = gather_context()
    prompt = ("Recent autonomous-agent activity in shared memory:\n\n" + ctx +
              "\n\nSurface at most one skill worth creating.")
    try:
        raw = asi.ask(prompt, system=SYSTEM, temperature=0.5, max_tokens=900).strip()
    except asi.ASIError as e:
        asi.log_run("dream", "error", f"LLM failed: {e}")
        print("LLM failed:", e)
        return 1

    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        idea = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001
        asi.log_run("dream", "parse-error", f"could not parse: {raw[:200]}")
        print("parse error:", e, "| raw:", raw[:200])
        return 0

    if not idea.get("worth_it"):
        asi.log_run("dream", "no-idea", "no recurring pattern worth a skill")
        print("no skill worth creating")
        return 0

    skill_doc = json.dumps(idea, indent=2)
    asi.directive("dream:latest-skill", f"[{int(time.time())}]\n{skill_doc}")
    asi.log_run("dream", "skill-proposed", f"proposed skill: {idea.get('skill_name')}")
    print("Proposed skill:", idea.get("skill_name"))
    print(skill_doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
