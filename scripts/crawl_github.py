#!/usr/bin/env python3
import hashlib, io, json, os, tarfile, urllib.request, zipfile
from datetime import datetime, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

def gh_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Block-Store-Crawler/1.0 (blockmd)",
        "Accept": "application/vnd.github+json",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def load_set(path):
    if not os.path.exists(path):
        return set()
    return {x.strip().lower() for x in json.load(open(path, encoding="utf-8")) if x.strip()}

def extract_manifest(raw_bytes, url):
    if url.lower().endswith(".zip") or raw_bytes[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for name in zf.namelist():
                if name == "manifest.json" or (name.count("/") == 1 and name.endswith("/manifest.json")):
                    return json.loads(zf.read(name).decode("utf-8"))
    else:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tf:
            for m in tf.getmembers():
                n = m.name.lstrip("./")
                if n == "manifest.json" or (n.count("/") == 1 and n.endswith("/manifest.json")):
                    return json.loads(tf.extractfile(m).read().decode("utf-8"))
    return None

def main():
    blocklist = load_set("blocklist.json")
    verified = load_set("verified.json")
    prev_dl = {}
    if os.path.exists("store-index.json"):
        for a in json.load(open("store-index.json", encoding="utf-8")).get("apps", []):
            prev_dl[a.get("id")] = int(a.get("downloads") or 0)

    search = json.loads(gh_get(f"{API}/search/repositories?q=topic:blockmd-plugin+fork:false+archived:false&sort=stars&order=desc&per_page=100"))
    apps, seen = [], set()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for repo in search.get("items", []):
        full = repo["full_name"]
        if full.lower() in blocklist:
            continue
        dl_url, gh_downloads, updated = "", 0, repo.get("pushed_at") or now_iso
        try:
            rels = json.loads(gh_get(f"{API}/repos/{full}/releases"))
            for r in rels:
                for ast in r.get("assets", []):
                    gh_downloads += int(ast.get("download_count") or 0)
            if rels:
                latest = rels[0]
                updated = latest.get("published_at") or updated
                for ast in latest.get("assets", []):
                    if ast["name"].endswith((".tar.gz", ".tgz", ".zip")):
                        dl_url = ast["browser_download_url"]
                        break
                if not dl_url:
                    dl_url = latest.get("tarball_url") or ""
        except Exception:
            pass
        if not dl_url:
            branch = repo.get("default_branch") or "main"
            dl_url = f"https://codeload.github.com/{full}/tar.gz/{branch}"

        try:
            raw = gh_get(dl_url)
            sha_hex = hashlib.sha256(raw).hexdigest()
            m = extract_manifest(raw, dl_url)
            if not m or m.get("app") != "blockmd" or m.get("id") in seen:
                continue
            pid = m["id"]
            if pid.lower() in blocklist:
                continue
            seen.add(pid)
            apps.append({
                "app": "blockmd",
                "id": pid,
                "name": m["name"],
                "version": m["version"],
                "author": m["author"],
                "repo": full,
                "summary": m["summary"],
                "download_url": dl_url,
                "sha256": sha_hex,
                "downloads": max(prev_dl.get(pid, 0), gh_downloads),
                "stars": int(repo.get("stargazers_count") or 0),
                "verified": pid.lower() in verified or full.lower() in verified,
                "updated_at": updated,
                "permissions": m.get("permissions", []),
                "surfaces": m.get("surfaces", ["settings_page"]),
                "icon": m["icon"],
            })
        except Exception as exc:
            print(f"[WARN] skip {full}: {exc}")

    apps.sort(key=lambda x: (not x["verified"], -x.get("downloads", 0), -x.get("stars", 0), x["id"]))
    out = {"app": "blockmd", "schema_version": 1, "topic": "blockmd-plugin", "updated_at": now_iso, "apps": apps}
    with open("store-index.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

if __name__ == "__main__":
    main()
