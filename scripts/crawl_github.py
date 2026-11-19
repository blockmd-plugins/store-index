#!/usr/bin/env python3
import concurrent.futures, hashlib, io, json, os, sys, tarfile, urllib.request, zipfile
from datetime import datetime, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
RESERVED_IDS = {"block-diagnostics"}
MAX_README_BYTES = 512 * 1024


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


def load_verified_map(path):
    if not os.path.exists(path):
        raise ValueError(f"verified map not found: {path}")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("verified.json must be a non-empty JSON object mapping id -> owner/repo")
    id_to_repo = {}
    repo_to_id = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"invalid verified entry {k!r}: {v!r}")
        pid = k.strip().lower()
        repo = v.strip().lower()
        parts = repo.split("/")
        if not pid or len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"invalid verified mapping {k!r} -> {v!r}")
        if pid in RESERVED_IDS:
            raise ValueError(f"verified.json must not include reserved id: {pid}")
        if pid in id_to_repo:
            raise ValueError(f"duplicate verified id: {pid}")
        if repo in repo_to_id:
            raise ValueError(f"duplicate verified repo: {repo}")
        id_to_repo[pid] = repo
        repo_to_id[repo] = pid
    return id_to_repo, repo_to_id


def is_repo_allowed_for_id(pid, full_name, id_to_repo, repo_to_id):
    pid_key = pid.strip().lower()
    repo_key = full_name.strip().lower()
    if pid_key in id_to_repo:
        expected_repo = id_to_repo[pid_key]
        if repo_key != expected_repo:
            return False, False, f"id {pid!r} is bound to {expected_repo}, got {full_name}"
        return True, True, ""
    if repo_key in repo_to_id:
        expected_id = repo_to_id[repo_key]
        return False, False, f"verified repo {full_name} is bound to id {expected_id!r}, got {pid!r}"
    return True, False, ""


def contains_emoji(text):
    for ch in text:
        cp = ord(ch)
        if 0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F:
            return True
    return False


def contains_han(text):
    for ch in text:
        cp = ord(ch)
        if (
            0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF
            or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2CEAF
        ):
            return True
    return False


def find_chinese_char(text):
    for ch in text:
        cp = ord(ch)
        if contains_han(ch) or (0x3000 <= cp <= 0x303F) or (0xFF01 <= cp <= 0xFF5E):
            return ch
    return ""


def validate_official_bilingual(manifest, docs):
    for field in ("name", "summary"):
        loc = manifest.get(field)
        if not isinstance(loc, dict):
            raise ValueError(f"manifest.{field} must be an object with zh and en")
        for lang in ("zh", "en"):
            val = str(loc.get(lang) or "").strip()
            if not val:
                raise ValueError(f"manifest.{field}.{lang} is required for official plugins")
            if contains_emoji(val):
                raise ValueError(f"manifest.{field}.{lang} must not contain emoji")
    author = str(manifest.get("author") or "").strip()
    if not author or contains_emoji(author):
        raise ValueError("manifest.author is required and must not contain emoji")

    zh_raw = docs.get("README.md")
    en_raw = docs.get("README.en.md")
    if zh_raw is None:
        raise ValueError("official plugin archive missing README.md")
    if en_raw is None:
        raise ValueError("official plugin archive missing README.en.md")
    if len(zh_raw) > MAX_README_BYTES or len(en_raw) > MAX_README_BYTES:
        raise ValueError("official plugin README exceeds 512 KiB limit")
    zh_text = zh_raw.decode("utf-8").lstrip("\ufeff").strip()
    en_text = en_raw.decode("utf-8").lstrip("\ufeff").strip()
    if not zh_text or not contains_han(zh_text):
        raise ValueError("README.md must be non-empty Chinese text")
    if not en_text:
        raise ValueError("README.en.md must not be empty")
    bad = find_chinese_char(en_text)
    if bad:
        raise ValueError(f"README.en.md must be English-only, found {bad!r}")
    if zh_text == en_text:
        raise ValueError("README.en.md must not be identical to README.md")


def validate_apps_against_verified(apps, id_to_repo):
    errors = []
    by_id = {}
    for app in apps:
        pid = (app.get("id") or "").strip().lower()
        repo = (app.get("repo") or "").strip().lower()
        if not pid or not repo:
            errors.append(f"entry missing id or repo: {app!r}")
            continue
        if pid in RESERVED_IDS:
            errors.append(f"reserved app id must not be published: {pid}")
            continue
        if pid in by_id:
            errors.append(f"duplicate app id: {pid}")
            continue
        by_id[pid] = app
        if app.get("verified"):
            expected_repo = id_to_repo.get(pid)
            if expected_repo != repo:
                errors.append(f"verified app {pid!r} has repo {repo!r}, expected {expected_repo!r}")
    for pid, expected_repo in id_to_repo.items():
        app = by_id.get(pid)
        if not app:
            errors.append(f"missing verified app {pid!r} ({expected_repo})")
            continue
        actual_repo = (app.get("repo") or "").strip().lower()
        if actual_repo != expected_repo:
            errors.append(f"verified app {pid!r} bound to {actual_repo!r}, expected {expected_repo!r}")
        if app.get("verified") is not True:
            errors.append(f"verified app {pid!r} must have verified=true")
    if errors:
        raise ValueError("; ".join(errors))


def extract_manifest_and_docs(raw_bytes, url):
    files = {}
    if url.lower().endswith(".zip") or raw_bytes[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                norm = info.filename.lstrip("./")
                if norm.count("/") <= 1:
                    files[norm] = zf.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                norm = m.name.lstrip("./")
                if norm.count("/") <= 1:
                    fp = tf.extractfile(m)
                    if fp is not None:
                        files[norm] = fp.read()

    manifest_key = None
    for name in files:
        if name == "manifest.json" or (name.count("/") == 1 and name.endswith("/manifest.json")):
            manifest_key = name
            break
    if manifest_key is None:
        return None, {}
    prefix = manifest_key[:-len("manifest.json")]
    docs = {}
    for doc_name in ("README.md", "README.en.md"):
        doc_key = prefix + doc_name
        if doc_key in files:
            docs[doc_name] = files[doc_key]
    return json.loads(files[manifest_key].decode("utf-8")), docs


def build_index(search_items, prev_dl, blocklist, id_to_repo, repo_to_id, fetch_repo_fn, now_iso):
    apps, owners, ambiguous = [], {}, set()
    candidates = [repo for repo in search_items if repo["full_name"].lower() not in blocklist]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(repo, pool.submit(fetch_repo_fn, repo, now_iso)) for repo in candidates]
        for repo, future in futures:
            full = repo["full_name"]
            try:
                fetched = future.result()
                if len(fetched) == 6:
                    dl_url, sha_hex, gh_downloads, updated, m, docs = fetched
                else:
                    dl_url, sha_hex, gh_downloads, updated, m = fetched
                    docs = {}
                if not m or m.get("app") != "blockmd":
                    continue
                pid = (m.get("id") or "").strip()
                if not pid:
                    continue
                pid_key = pid.lower()
                if pid_key in RESERVED_IDS or pid_key in blocklist:
                    continue
                allowed, is_verified, reason = is_repo_allowed_for_id(pid, full, id_to_repo, repo_to_id)
                if not allowed:
                    print(f"[WARN] skip {full}: {reason}")
                    continue
                declared = str(m.get("repo") or "").strip().lower()
                if declared and declared != full.lower():
                    continue
                if is_verified or full.lower().startswith("blockmd-plugins/"):
                    validate_official_bilingual(m, docs)
                if pid_key in owners:
                    if owners[pid_key] != full.lower():
                        ambiguous.add(pid_key)
                    continue
                owners[pid_key] = full.lower()
                app_entry = {
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
                    "verified": is_verified,
                    "updated_at": updated,
                    "permissions": m.get("permissions", []),
                    "surfaces": m.get("surfaces", ["settings_page"]),
                    "icon": m["icon"],
                }
                if m.get("beta") is True:
                    app_entry["beta"] = True
                apps.append(app_entry)
            except Exception as exc:
                print(f"[WARN] skip {full}: {exc}")

    apps = [app for app in apps if app["id"].lower() not in ambiguous]
    validate_apps_against_verified(apps, id_to_repo)
    apps.sort(key=lambda x: (not x["verified"], -x.get("downloads", 0), -x.get("stars", 0), x["id"]))
    return {"app": "blockmd", "schema_version": 1, "topic": "blockmd-plugin", "updated_at": now_iso, "apps": apps}


def fetch_github_repo(repo, now_iso):
    full = repo["full_name"]
    dl_url, gh_downloads, updated = "", 0, repo.get("pushed_at") or now_iso
    try:
        rels = json.loads(gh_get(f"{API}/repos/{full}/releases?per_page=30"))
        for r in rels:
            for ast in r.get("assets", []):
                gh_downloads += int(ast.get("download_count") or 0)
        stable_rels = [r for r in rels if not r.get("draft") and not r.get("prerelease")]
        if stable_rels:
            latest = stable_rels[0]
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
    raw = gh_get(dl_url)
    sha_hex = hashlib.sha256(raw).hexdigest()
    m, docs = extract_manifest_and_docs(raw, dl_url)
    return dl_url, sha_hex, gh_downloads, updated, m, docs


def main():
    blocklist = load_set("blocklist.json")
    id_to_repo, repo_to_id = load_verified_map("verified.json")
    prev_dl = {}
    if os.path.exists("store-index.json"):
        for a in json.load(open("store-index.json", encoding="utf-8")).get("apps", []):
            prev_dl[a.get("id")] = int(a.get("downloads") or 0)

    search = json.loads(gh_get(f"{API}/search/repositories?q=topic:blockmd-plugin+fork:false+archived:false&sort=stars&order=desc&per_page=100"))
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = build_index(
        search.get("items", []),
        prev_dl,
        blocklist,
        id_to_repo,
        repo_to_id,
        fetch_github_repo,
        now_iso,
    )
    tmp_path = "store-index.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, "store-index.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
