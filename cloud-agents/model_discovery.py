#!/usr/bin/env python3
"""Cloud Model-Discovery agent (Phase 16 port of scripts/model-discovery/daemon.py).

Queries the HuggingFace Hub for recently-updated FREE text-generation models,
scores them, and records the best candidates into shared cloud memory (D1) so the
local blind-proxy (or any consumer) can pick them up. Runs on a GitHub-hosted
runner via cron — no local host required.

Env:
  HF_TOKEN   optional, raises HF rate limits
  (+ BRAIN_URL / PROXY_AUTH_TOKEN from asi_cloud)
"""
import json
import os
import time
import urllib.request

import asi_cloud as asi

HF_TOKEN = os.environ.get("HF_TOKEN", "")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"


def hf_recent(limit=40):
    url = ("https://huggingface.co/api/models?sort=lastModified&direction=-1"
           f"&limit={limit}&filter=text-generation&full=false")
    headers = {"User-Agent": UA}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def score(m):
    s = 0.0
    likes = m.get("likes", 0) or 0
    downloads = m.get("downloads", 0) or 0
    s += min(likes, 500) * 0.5
    s += min(downloads, 100000) / 1000.0
    mid = (m.get("id") or "").lower()
    for kw, w in (("instruct", 30), ("chat", 25), ("coder", 35), ("reasoning", 30),
                  ("70b", 20), ("32b", 15), ("moe", 15)):
        if kw in mid:
            s += w
    tags = [t.lower() for t in (m.get("tags") or [])]
    if any(t in tags for t in ("apache-2.0", "mit", "llama3", "gemma")):
        s += 20
    return round(s, 1)


def main():
    try:
        models = hf_recent()
    except Exception as e:  # noqa: BLE001
        asi.log_run("model-discovery", "error", f"HF query failed: {e}")
        print("HF query failed:", e)
        return 1

    scored = sorted(
        ({"id": m.get("id"), "score": score(m),
          "likes": m.get("likes", 0), "downloads": m.get("downloads", 0)}
         for m in models if m.get("id")),
        key=lambda x: x["score"], reverse=True,
    )[:10]

    report = "\n".join(f"  {c['score']:>6}  {c['id']}" for c in scored)
    print("Top free-model candidates:\n" + report)

    payload = json.dumps({"generated_at": int(time.time()), "candidates": scored})
    asi.d1_write(
        "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        ["model-discovery:candidates", payload, int(time.time())],
    )
    asi.log_run("model-discovery", "ok",
                f"{len(scored)} candidates; top: {scored[0]['id'] if scored else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
