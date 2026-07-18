#!/usr/bin/env python3
"""asi_cloud — shared runtime for the cloud-native AI-Ops agents (Phase 16).

Replaces the local-only primitives (127.0.0.1 blind-proxy, local-subagent-runner,
systemd journals) with cloud equivalents so the agents survive local poweroff:

  * LLM        -> cloud-brain-proxy Worker  (OpenAI-compatible /v1/chat/completions)
  * memory     -> D1 gateway on the SAME Worker (bearer-gated /d1/* routes)
  * transport  -> plain HTTPS on a GitHub-hosted runner (NO Tor: the runner is
                  already an ephemeral, disposable cloud VM; §2.2 fail-closed
                  applies to THIS host's egress, not to a throwaway CI box).

Env (set as GitHub repo secrets):
  BRAIN_URL          cloud-brain-proxy base URL (no trailing slash)
  PROXY_AUTH_TOKEN   bearer token for the Worker (LLM + D1 routes)
  BRAIN_MODEL        optional model id (default: cloud-brain-proxy/auto)
"""
import json
import os
import time
import urllib.request
import urllib.error

BRAIN_URL = os.environ.get("BRAIN_URL", "").rstrip("/")
AUTH = os.environ.get("PROXY_AUTH_TOKEN", "")
MODEL = os.environ.get("BRAIN_MODEL", "cloud-brain-proxy/auto")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class ASIError(RuntimeError):
    pass


def _post(path, body, timeout=90, retries=4):
    if not BRAIN_URL:
        raise ASIError("BRAIN_URL not set")
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
    if AUTH:
        headers["Authorization"] = f"Bearer {AUTH}"
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(BRAIN_URL + path, data=data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                if raw.lstrip()[:15].lower().startswith("<!doctype html"):
                    last = ASIError("edge returned HTML challenge")
                    time.sleep(2 + attempt * 2)
                    continue
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last = ASIError(f"HTTP {e.code}: {e.read()[:200]!r}")
        except Exception as e:  # noqa: BLE001
            last = ASIError(f"{type(e).__name__}: {e}")
        time.sleep(2 + attempt * 2)
    raise last or ASIError("request failed")


# ---- LLM -------------------------------------------------------------------
def ask(prompt, system=None, model=None, temperature=0.4, max_tokens=2048):
    """One-shot completion via cloud-brain-proxy. Returns the assistant text."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {
        "model": model or MODEL,
        "messages": msgs,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    j = _post("/v1/chat/completions", body, timeout=120)
    try:
        return j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ASIError(f"unexpected LLM response: {json.dumps(j)[:300]}")


# ---- shared memory (D1 via Worker) -----------------------------------------
def d1_query(sql, params=None):
    j = _post("/d1/query", {"sql": sql, "params": params or []})
    if not j.get("success"):
        raise ASIError(f"d1_query failed: {j.get('error')}")
    return j.get("results", [])


def d1_write(sql, params=None):
    j = _post("/d1/write", {"sql": sql, "params": params or []})
    if not j.get("success"):
        raise ASIError(f"d1_write failed: {j.get('error')}")
    return j.get("meta", {})


def directive(section, content):
    """Upsert a memory section (mirrors rule.md sections into shared cloud memory)."""
    j = _post("/d1/directive", {"section": section, "content": content})
    if not j.get("success"):
        raise ASIError(f"directive failed: {j.get('error')}")
    return True


def log_run(agent, status, summary):
    """Append an agent run record to the shared kv log + a per-agent directive."""
    ts = int(time.time())
    try:
        d1_write(
            "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            [f"agent:{agent}:last", json.dumps({"status": status, "summary": summary[:2000], "ts": ts}), ts],
        )
    except ASIError as e:
        print(f"[warn] log_run kv failed: {e}")
    try:
        directive(f"cloud-agent:{agent}", f"[{ts}] {status}\n{summary[:8000]}")
    except ASIError as e:
        print(f"[warn] log_run directive failed: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        print("BRAIN_URL:", BRAIN_URL or "(unset)")
        print("ping:", ask("Reply with exactly: PONG", max_tokens=8))
        print("d1:", d1_query("SELECT COUNT(*) AS n FROM directives"))
