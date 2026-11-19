#!/usr/bin/env python3
import hashlib, io, json, os, posixpath, re, stat, sys, tarfile, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
RESERVED_IDS = {"block-diagnostics"}
PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
VALID_PERMISSIONS = {
    "storage",
    "notes:read",
    "notes:write",
    "network",
    "events:note_saved",
    "ui:titlebar",
    "ui:settings",
    "ui:commands",
    "ui:panels",
    "ui:status",
    "ui:notify",
    "editor:read",
    "editor:write",
    "events:workspace",
}
VALID_SURFACES = {"settings_page"}
MAX_API_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 8192
MAX_MANIFEST_BYTES = 256 * 1024
MAX_README_BYTES = 512 * 1024


def read_limited(resp, limit, label):
    chunks = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"{label} exceeds limit ({limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def gh_api_get(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValueError(f"refusing API request to unexpected host: {url}")
    headers = {
        "User-Agent": "Block-Store-Crawler/1.0 (blockmd)",
        "Accept": "application/vnd.github+json",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return read_limited(r, MAX_API_BYTES, "GitHub API response")


def download_archive(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"invalid archive URL: {url}")
    # Never forward GITHUB_TOKEN when downloading public release/source archives.
    req = urllib.request.Request(url, headers={
        "User-Agent": "Block-Store-Crawler/1.0 (blockmd)",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > MAX_ARCHIVE_BYTES:
            raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")
        return read_limited(r, MAX_ARCHIVE_BYTES, "plugin archive")


def json_loads_strict(raw_text):
    def no_dup_pairs(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError(f"duplicate JSON key: {k!r}")
            obj[k] = v
        return obj
    return json.loads(raw_text, object_pairs_hook=no_dup_pairs)


def load_set(path):
    if not os.path.exists(path):
        return set()
    return {x.strip().lower() for x in json.load(open(path, encoding="utf-8")) if x.strip()}


def load_verified_map(path):
    if not os.path.exists(path):
        raise ValueError(f"verified map not found: {path}")
    with open(path, encoding="utf-8") as f:
        raw = json_loads_strict(f.read())
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
        if (
            contains_han(ch)
            or (0x3000 <= cp <= 0x303F)
            or (0xFE10 <= cp <= 0xFE1F)
            or (0xFE30 <= cp <= 0xFE4F)
            or (0xFF01 <= cp <= 0xFF5E)
        ):
            return ch
    return ""


def contains_ascii_letter(text):
    return any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text)


def strip_html_comments(text):
    text = HTML_COMMENT_RE.sub("", text)
    idx = text.find("<!--")
    if idx >= 0:
        text = text[:idx]
    return text.strip()


def validate_manifest_core(m):
    if not isinstance(m, dict):
        raise ValueError("manifest must be a JSON object")
    if m.get("app") != "blockmd":
        raise ValueError(f"manifest.app must be 'blockmd', got {m.get('app')!r}")
    if m.get("schema_version") != 1:
        raise ValueError(f"manifest.schema_version must be 1, got {m.get('schema_version')!r}")
    pid = str(m.get("id") or "").strip()
    if not PLUGIN_ID_RE.match(pid):
        raise ValueError(f"invalid manifest.id: {pid!r}")
    version = str(m.get("version") or "").strip()
    if not SEMVER_RE.match(version):
        raise ValueError(f"invalid manifest.version: {version!r}")
    author = str(m.get("author") or "").strip()
    if not author or contains_emoji(author):
        raise ValueError("manifest.author is required and must not contain emoji")
    repo = str(m.get("repo") or "").strip()
    if repo and not REPO_SLUG_RE.match(repo):
        raise ValueError(f"invalid manifest.repo: {repo!r}")

    for field, limits in (("name", None), ("summary", (80, 140))):
        loc = m.get(field)
        if not isinstance(loc, dict):
            raise ValueError(f"manifest.{field} must be an object with zh and en")
        zh = str(loc.get("zh") or "").strip()
        en = str(loc.get("en") or "").strip()
        if not zh or not en:
            raise ValueError(f"manifest.{field}.zh and manifest.{field}.en are both required")
        if contains_emoji(zh) or contains_emoji(en):
            raise ValueError(f"manifest.{field} must not contain emoji")
        if limits and (len(zh) > limits[0] or len(en) > limits[1]):
            raise ValueError(f"manifest.{field} exceeds length limit")

    entry = m.get("entry")
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), list) or not entry["command"]:
        raise ValueError("manifest.entry.command must be a non-empty array")
    if not all(isinstance(x, str) for x in entry["command"]) or not entry["command"][0].strip():
        raise ValueError("manifest.entry.command must contain only strings with a non-empty executable")

    perms = m.get("permissions")
    if not isinstance(perms, list):
        raise ValueError("manifest.permissions must be a list")
    seen_perms = set()
    for p in perms:
        if not isinstance(p, str) or p not in VALID_PERMISSIONS or p in seen_perms:
            raise ValueError(f"invalid or duplicate manifest permission: {p!r}")
        seen_perms.add(p)

    surfaces = m.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        raise ValueError("manifest.surfaces must declare ['settings_page']")
    seen_surfaces = set()
    for s in surfaces:
        if not isinstance(s, str) or s not in VALID_SURFACES or s in seen_surfaces:
            raise ValueError(f"invalid or duplicate manifest surface: {s!r}")
        seen_surfaces.add(s)

    icon = m.get("icon")
    if not isinstance(icon, dict) or icon.get("view_box") != "0 0 24 24":
        raise ValueError("manifest.icon.view_box must be '0 0 24 24'")
    strokes = icon.get("stroke_paths")
    if not isinstance(strokes, list) or not strokes or not all(isinstance(x, str) and x.strip() for x in strokes):
        raise ValueError("manifest.icon.stroke_paths must contain at least one SVG path")


def validate_localized_field(path_label, loc):
    if not isinstance(loc, dict):
        raise ValueError(f"{path_label} must be an object with zh and en")
    for lang in ("zh", "en"):
        val = str(loc.get(lang) or "").strip()
        if not val:
            raise ValueError(f"{path_label}.{lang} is required for official plugins")
        if contains_emoji(val):
            raise ValueError(f"{path_label}.{lang} must not contain emoji")
    en_val = str(loc.get("en") or "").strip()
    bad = find_chinese_char(en_val)
    if bad:
        raise ValueError(f"{path_label}.en must be English-only, found {bad!r}")


def validate_official_bilingual(manifest, docs):
    for field in ("name", "summary"):
        validate_localized_field(f"manifest.{field}", manifest.get(field))

    contributes = manifest.get("contributes")
    if isinstance(contributes, dict):
        for group, key in (
            ("titlebar_buttons", "tooltip"),
            ("settings_sections", "title"),
            ("commands", "title"),
            ("page_menu", "title"),
            ("editor_menu", "title"),
            ("panels", "title"),
            ("dialogs", "title"),
        ):
            for idx, item in enumerate(contributes.get(group) or []):
                if isinstance(item, dict) and key in item:
                    validate_localized_field(f"manifest.contributes.{group}[{idx}].{key}", item.get(key))
        for idx, item in enumerate(contributes.get("status_items") or []):
            if isinstance(item, dict) and item.get("text") is not None:
                text_obj = item.get("text")
                if isinstance(text_obj, dict) and (str(text_obj.get("zh") or "").strip() or str(text_obj.get("en") or "").strip()):
                    validate_localized_field(f"manifest.contributes.status_items[{idx}].text", text_obj)

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
    zh_visible = strip_html_comments(zh_text)
    if not zh_visible or not contains_han(zh_visible):
        raise ValueError("README.md must contain visible Chinese text")
    if not en_text:
        raise ValueError("README.en.md must not be empty")
    bad = find_chinese_char(en_text)
    if bad:
        raise ValueError(f"README.en.md must be English-only, found {bad!r}")
    en_visible = strip_html_comments(en_text)
    if not contains_ascii_letter(en_visible):
        raise ValueError("README.en.md must contain visible English text")
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


def _normalize_archive_path(raw_name):
    if "\\" in raw_name or raw_name.startswith("/"):
        raise ValueError(f"unsafe archive path: {raw_name!r}")
    raw_parts = raw_name.split("/")
    if ".." in raw_parts:
        raise ValueError(f"unsafe archive path: {raw_name!r}")
    norm = posixpath.normpath(raw_name)
    if not norm or norm in (".", "..") or norm.startswith("/") or norm.startswith("../"):
        raise ValueError(f"unsafe archive path: {raw_name!r}")
    return norm


def _read_bounded(stream, limit, label):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return data


def extract_manifest_and_docs(raw_bytes, url):
    files = {}
    seen_paths = set()
    entry_count = 0
    total_extracted = 0

    if url.lower().endswith(".zip") or raw_bytes[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for info in zf.infolist():
                entry_count += 1
                if entry_count > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("archive exceeds max entry limit")
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type != 0 and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(f"non-regular file in zip archive: {info.filename!r}")
                if info.is_dir():
                    if file_type == stat.S_IFREG:
                        raise ValueError(f"invalid directory entry in zip archive: {info.filename!r}")
                    continue
                if file_type == stat.S_IFDIR:
                    continue
                if info.file_size > MAX_SINGLE_FILE_BYTES:
                    raise ValueError(f"archive member too large: {info.filename!r}")
                total_extracted += info.file_size
                if total_extracted > MAX_TOTAL_EXTRACTED_BYTES:
                    raise ValueError("archive extracted size exceeds limit")
                norm = _normalize_archive_path(info.filename)
                if norm in seen_paths:
                    raise ValueError(f"duplicate archive path: {norm!r}")
                seen_paths.add(norm)
                base = norm.rsplit("/", 1)[-1]
                if norm.count("/") <= 1 and base in ("manifest.json", "README.md", "README.en.md"):
                    limit = MAX_MANIFEST_BYTES if base == "manifest.json" else MAX_README_BYTES
                    with zf.open(info, "r") as fp:
                        files[norm] = _read_bounded(fp, limit, base)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tf:
            for m in tf.getmembers():
                entry_count += 1
                if entry_count > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("archive exceeds max entry limit")
                if m.isdir():
                    continue
                if not m.isreg():
                    raise ValueError(f"non-regular file in tar archive: {m.name!r}")
                if m.size > MAX_SINGLE_FILE_BYTES:
                    raise ValueError(f"archive member too large: {m.name!r}")
                total_extracted += m.size
                if total_extracted > MAX_TOTAL_EXTRACTED_BYTES:
                    raise ValueError("archive extracted size exceeds limit")
                norm = _normalize_archive_path(m.name)
                if norm in seen_paths:
                    raise ValueError(f"duplicate archive path: {norm!r}")
                seen_paths.add(norm)
                base = norm.rsplit("/", 1)[-1]
                if norm.count("/") <= 1 and base in ("manifest.json", "README.md", "README.en.md"):
                    limit = MAX_MANIFEST_BYTES if base == "manifest.json" else MAX_README_BYTES
                    fp = tf.extractfile(m)
                    if fp is not None:
                        files[norm] = _read_bounded(fp, limit, base)

    manifest_keys = [
        name for name in files
        if name == "manifest.json" or (name.count("/") == 1 and name.endswith("/manifest.json"))
    ]
    if len(manifest_keys) == 0:
        return None, {}
    if len(manifest_keys) > 1:
        raise ValueError(f"archive contains multiple manifest.json candidates: {manifest_keys}")
    manifest_key = manifest_keys[0]
    prefix = manifest_key[:-len("manifest.json")]
    docs = {}
    for doc_name in ("README.md", "README.en.md"):
        doc_key = prefix + doc_name
        if doc_key in files:
            docs[doc_name] = files[doc_key]
    return json_loads_strict(files[manifest_key].decode("utf-8")), docs


def build_index(search_items, prev_dl, blocklist, id_to_repo, repo_to_id, fetch_repo_fn, now_iso):
    apps, owners, ambiguous = [], {}, set()
    for repo in search_items:
        full = repo["full_name"]
        if full.lower() in blocklist:
            continue
        try:
            fetched = fetch_repo_fn(repo, now_iso)
            if len(fetched) == 6:
                dl_url, sha_hex, gh_downloads, updated, m, docs = fetched
            else:
                dl_url, sha_hex, gh_downloads, updated, m = fetched
                docs = {}
            if not m:
                continue
            validate_manifest_core(m)
            pid = m["id"].strip()
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
        rels = json.loads(gh_api_get(f"{API}/repos/{full}/releases"))
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
    raw = download_archive(dl_url)
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

    search = json.loads(gh_api_get(f"{API}/search/repositories?q=topic:blockmd-plugin+fork:false+archived:false&sort=stars&order=desc&per_page=100"))
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
