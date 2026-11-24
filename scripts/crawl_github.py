#!/usr/bin/env python3
import hashlib, io, json, os, posixpath, re, stat, sys, tarfile, unicodedata, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone

_GO_LATIN_SPACES = {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0}


def _is_go_space(ch):
    cp = ord(ch)
    if cp in _GO_LATIN_SPACES:
        return True
    return unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def _go_trim_space(s):
    start = 0
    end = len(s)
    while start < end and _is_go_space(s[start]):
        start += 1
    while end > start and _is_go_space(s[end - 1]):
        end -= 1
    return s[start:end]


_GO_LOWER_OVERRIDES = {
    0x0130: "i",
    0x1C89: "\u1c8a",
    0xA7CB: "\u0264",
    0xA7CC: "\ua7cd",
    0xA7CE: "\ua7cf",
    0xA7D2: "\ua7d3",
    0xA7D4: "\ua7d5",
    0xA7DA: "\ua7db",
    0xA7DC: "\u019b",
}


def _go_lower(s):
    # Matches Go 1.27 unicode.ToLower across all 1.1M Unicode code points on Python 3.11-3.15
    # (single-rune lowercase: no contextual Greek final sigma, U+0130 'İ' -> 'i',
    # plus Unicode 16/17 Garay & Beria Erfe additions for older Python unicodedata).
    out = []
    for ch in s:
        cp = ord(ch)
        ov = _GO_LOWER_OVERRIDES.get(cp)
        if ov is not None:
            out.append(ov)
        elif 0x10D50 <= cp <= 0x10D65:
            out.append(chr(cp + 0x20))
        elif 0x16EA0 <= cp <= 0x16EB8:
            out.append(chr(cp + 0x1B))
        else:
            out.append(ch.lower())
    return "".join(out)

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
    "ui:sidebar",
    "ui:appearance",
    "ui:menubar",
    "editor:read",
    "editor:write",
    "events:workspace",
    "notes:path",
    "system:notify",
    "system:secrets",
    "system:open",
    "system:dialogs",
    "system:clipboard_read",
    "system:clipboard_write",
    "system:trash",
    "system:badge",
    "events:system",
}
VALID_CATEGORIES = {"sync", "edit", "organize", "ai", "publish", "appearance", "tools", "language"}
RETIRED_CATEGORIES = {"sync", "publish"}
VALID_SURFACES = {"settings_page"}
LEGACY_RELEASES = {"blockmd-plugins/block-sync": "v1.3.44"}
RUNTIME_CONTRIB_KEYS = (
    "titlebar_buttons",
    "settings_sections",
    "commands",
    "page_menu",
    "editor_menu",
    "sidebar_menu",
    "tab_menu",
    "panels",
    "sidebar_sections",
    "file_badges",
    "backdrops",
    "status_items",
    "dialogs",
)
MAX_API_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 8192
MAX_MANIFEST_BYTES = 256 * 1024
MAX_README_BYTES = 512 * 1024
MAX_CHANGELOG_INLINE = 5
MAX_CHANGELOG_FULL = 20
MAX_RELEASE_BODY_BYTES = 8192
CHANGELOG_BASE_URL = "https://raw.githubusercontent.com/blockmd-plugins/store-index/main/changelogs"


def _looks_chinese(text):
    if any(k in text for k in ("## 中文", "本次更新重点", "### 新功能", "### 改进", "### 修复", "✨ 新功能")):
        return True
    return any(0x4E00 <= ord(ch) <= 0x9FFF for ch in text)


def _parse_single_notes(text):
    out = {}
    highlights = ""
    buckets = {"new": [], "improvements": [], "fixes": [], "other": []}
    section = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("Website:", "官网：", "官网:")):
            section = "skip"
            continue
        if not highlights and (line.startswith("🌈") or "本次更新重点" in line or "Highlights" in line):
            hl = line.lstrip("🌈# ").strip()
            for prefix in ("本次更新重点：", "本次更新重点:", "Highlights:", "Highlights："):
                if hl.startswith(prefix):
                    hl = hl[len(prefix):]
            highlights = hl.strip()
            continue
        if line.startswith(("#", "✨", "🪐", "🐛")):
            h = line.lstrip("#✨🪐🐛 ").strip()
            h_low = h.lower()
            if h_low == "english" or h == "中文":
                continue
            if h_low in ("download", "install") or "下载" in h or "安装" in h:
                section = "skip"
            elif h_low == "new" or "新功能" in h or h_low.startswith("new ") or h_low == "features":
                section = "new"
            elif h_low in ("improvements", "improved") or "改进" in h or "优化" in h:
                section = "improvements"
            elif h_low in ("fixes", "fixed") or "修复" in h:
                section = "fixes"
            elif h_low in ("other", "changes") or "其他" in h or "改动" in h:
                section = "other"
            elif line.startswith("✨"):
                section = "new"
            elif line.startswith("🪐"):
                section = "improvements"
            elif line.startswith("🐛"):
                section = "fixes"
            continue
        if section == "skip":
            continue
        bullet = ""
        if line.startswith("- "):
            bullet = line[2:].strip()
        elif line.startswith("* "):
            bullet = line[2:].strip()
        elif line.startswith("• "):
            bullet = line[2:].strip()
        elif line.startswith("•"):
            bullet = line[1:].strip()
        if not bullet:
            continue
        bullet = bullet.replace("**", "")
        key = section if section in buckets else "other"
        buckets[key].append(bullet)
    if highlights:
        out["highlights"] = highlights
    for k in ("new", "improvements", "fixes", "other"):
        if buckets[k]:
            out[k] = buckets[k]
    return out


def parse_bilingual_release_body(raw):
    norm = (raw or "").replace("\r\n", "\n").strip()
    if not norm:
        return {}, {}
    idx = norm.find("\n---\n")
    sep_len = 5
    if idx < 0:
        idx = norm.find("\n---")
        sep_len = 4
    if idx < 0:
        parsed = _parse_single_notes(norm)
        if _looks_chinese(norm):
            return parsed, {}
        return {}, parsed
    left = norm[:idx].strip()
    right = norm[idx + sep_len:].strip()
    if not right:
        parsed = _parse_single_notes(left)
        return (parsed, {}) if _looks_chinese(left) else ({}, parsed)
    if _looks_chinese(right) and not _looks_chinese(left):
        return _parse_single_notes(right), _parse_single_notes(left)
    if _looks_chinese(left) and not _looks_chinese(right):
        return _parse_single_notes(left), _parse_single_notes(right)
    return _parse_single_notes(right), _parse_single_notes(left)


def normalize_release_entry(rel, include_body=True):
    tag = str(rel.get("tag_name") or "").strip()
    if not tag:
        return None
    version = tag[1:] if tag.startswith(("v", "V")) else tag
    body = str(rel.get("body") or "").replace("\r\n", "\n").strip()
    if len(body.encode("utf-8")) > MAX_RELEASE_BODY_BYTES:
        body = body.encode("utf-8")[:MAX_RELEASE_BODY_BYTES].decode("utf-8", errors="ignore").rstrip()
    zh, en = parse_bilingual_release_body(body)
    entry = {"version": version, "tag": tag}
    pub = str(rel.get("published_at") or rel.get("created_at") or "").strip()
    if pub:
        entry["published_at"] = pub
    html_url = str(rel.get("html_url") or "").strip()
    if html_url:
        entry["html_url"] = html_url
    if zh:
        entry["zh"] = zh
    if en:
        entry["en"] = en
    if body and (include_body or (not zh and not en)):
        entry["body"] = body
    return entry


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

    contributes = m.get("contributes")
    if contributes is not None and not isinstance(contributes, dict):
        raise ValueError("manifest.contributes must be an object")
    if isinstance(contributes, dict):
        for ck in (*RUNTIME_CONTRIB_KEYS, "languages"):
            val = contributes.get(ck)
            if val is not None and not isinstance(val, list):
                raise ValueError(f"manifest.contributes.{ck} must be a list")
    has_languages = isinstance(contributes, dict) and isinstance(contributes.get("languages"), list) and len(contributes["languages"]) > 0
    raw_cat_field = m.get("category")
    if raw_cat_field is not None and not isinstance(raw_cat_field, str):
        raise ValueError("manifest.category must be a string")
    raw_cat = raw_cat_field or ""
    if raw_cat != "" and (raw_cat != raw_cat.strip() or raw_cat != raw_cat.lower() or raw_cat not in VALID_CATEGORIES):
        raise ValueError(f"invalid manifest.category: {raw_cat!r}")
    if has_languages and raw_cat != "language":
        raise ValueError("manifest.category must be 'language' when contributes.languages is declared")

    perms = m.get("permissions")
    if perms is None and has_languages:
        perms = []
    if not isinstance(perms, list):
        raise ValueError("manifest.permissions must be a list")
    seen_perms = set()
    for p in perms:
        if not isinstance(p, str) or p not in VALID_PERMISSIONS or p in seen_perms:
            raise ValueError(f"invalid or duplicate manifest permission: {p!r}")
        seen_perms.add(p)

    surfaces = m.get("surfaces")
    if surfaces is None and has_languages:
        surfaces = []
    if not isinstance(surfaces, list):
        raise ValueError("manifest.surfaces must be a list")
    entry = m.get("entry")
    if entry is not None and not isinstance(entry, dict):
        raise ValueError("manifest.entry must be an object")
    entry_cmd = entry.get("command") if isinstance(entry, dict) else None
    if entry_cmd is not None:
        if not isinstance(entry_cmd, list) or not all(isinstance(x, str) for x in entry_cmd):
            raise ValueError("manifest.entry.command must be an array of strings")
    has_entry_cmd = isinstance(entry_cmd, list) and len(entry_cmd) > 0
    has_runtime_contribs = isinstance(contributes, dict) and any(
        isinstance(contributes.get(k), list) and len(contributes[k]) > 0
        for k in RUNTIME_CONTRIB_KEYS
    )
    declarative = (
        not has_entry_cmd
        and len(perms) == 0
        and len(surfaces) == 0
        and raw_cat == "language"
        and has_languages
        and not has_runtime_contribs
    )
    if not declarative:
        if not has_entry_cmd or not entry_cmd[0].strip():
            raise ValueError("manifest.entry.command must be a non-empty array of strings with a non-empty executable")
        if not surfaces:
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


def validate_official_category(manifest):
    # Official plugins must choose their store tab themselves; retired
    # values ("sync", "publish") are accepted for third-party plugins only.
    cat = str(manifest.get("category") or "").strip().lower()
    if not cat:
        raise ValueError("official plugin must declare manifest.category")
    if cat in RETIRED_CATEGORIES:
        raise ValueError(f"official plugin uses retired manifest.category {cat!r}; use 'tools'")


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
            ("sidebar_menu", "title"),
            ("tab_menu", "title"),
            ("sidebar_sections", "title"),
            ("file_badges", "tooltip"),
            ("backdrops", "title"),
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
    perm_reasons = manifest.get("permission_reasons")
    if isinstance(perm_reasons, dict):
        for perm_key, reason_obj in perm_reasons.items():
            validate_localized_field(f"manifest.permission_reasons[{perm_key}]", reason_obj)

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


def _record_dir_entry(raw_name, seen_dirs, seen_files):
    if not raw_name or "\\" in raw_name or raw_name.startswith("/"):
        raise ValueError(f"unsafe archive directory path: {raw_name!r}")
    stripped = raw_name.rstrip("/")
    if not stripped:
        raise ValueError(f"unsafe archive directory path: {raw_name!r}")
    if stripped == ".":
        return
    norm = _normalize_archive_path(stripped)
    if norm in seen_files:
        raise ValueError(f"archive directory conflicts with file: {norm!r}")
    parts = norm.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        if prefix in seen_files:
            raise ValueError(f"archive directory conflicts with file: {prefix!r}")
        seen_dirs.add(prefix)


def _record_file_entry(raw_name, seen_dirs, seen_files):
    norm = _normalize_archive_path(raw_name)
    if norm in seen_files:
        raise ValueError(f"duplicate archive path: {norm!r}")
    if norm in seen_dirs:
        raise ValueError(f"archive file conflicts with directory: {norm!r}")
    parts = norm.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if parent in seen_files:
            raise ValueError(f"archive file parent conflicts with file: {parent!r}")
        seen_dirs.add(parent)
    seen_files.add(norm)
    return norm


def extract_manifest_and_docs(raw_bytes, url):
    files = {}
    seen_dirs = set()
    seen_files = set()
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
                    _record_dir_entry(info.filename, seen_dirs, seen_files)
                    continue
                if file_type == stat.S_IFDIR:
                    _record_dir_entry(info.filename, seen_dirs, seen_files)
                    continue
                if info.file_size > MAX_SINGLE_FILE_BYTES:
                    raise ValueError(f"archive member too large: {info.filename!r}")
                total_extracted += info.file_size
                if total_extracted > MAX_TOTAL_EXTRACTED_BYTES:
                    raise ValueError("archive extracted size exceeds limit")
                norm = _record_file_entry(info.filename, seen_dirs, seen_files)
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
                    _record_dir_entry(m.name, seen_dirs, seen_files)
                    continue
                if not m.isreg():
                    raise ValueError(f"non-regular file in tar archive: {m.name!r}")
                if m.size > MAX_SINGLE_FILE_BYTES:
                    raise ValueError(f"archive member too large: {m.name!r}")
                total_extracted += m.size
                if total_extracted > MAX_TOTAL_EXTRACTED_BYTES:
                    raise ValueError("archive extracted size exceeds limit")
                norm = _record_file_entry(m.name, seen_dirs, seen_files)
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


def build_index(search_items, prev_dl, blocklist, id_to_repo, repo_to_id, fetch_repo_fn, now_iso, require_official_category=True):
    apps, owners, ambiguous = [], {}, set()
    for repo in search_items:
        full = repo["full_name"]
        if full.lower() in blocklist:
            continue
        try:
            fetched = fetch_repo_fn(repo, now_iso)
            releases = []
            if len(fetched) == 7:
                dl_url, sha_hex, gh_downloads, updated, m, docs, releases = fetched
            elif len(fetched) == 6:
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
                if require_official_category:
                    validate_official_category(m)
            if pid_key in owners:
                if owners[pid_key] != full.lower():
                    ambiguous.add(pid_key)
                continue
            owners[pid_key] = full.lower()
            raw_cat = str(m.get("category") or "").strip().lower()
            if raw_cat and raw_cat not in VALID_CATEGORIES:
                raise ValueError(f"invalid manifest.category: {raw_cat!r}")
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
                "permissions": m.get("permissions") or [],
                "surfaces": m.get("surfaces") if isinstance(m.get("surfaces"), list) else [],
                "icon": m["icon"],
            }
            if raw_cat:
                app_entry["category"] = raw_cat
            if isinstance(m.get("permission_reasons"), dict) and m["permission_reasons"]:
                app_entry["permission_reasons"] = m["permission_reasons"]
            contribs = m.get("contributes")
            if isinstance(contribs, dict) and isinstance(contribs.get("languages"), list):
                tags = []
                seen_tags = set()
                for lang in contribs["languages"]:
                    if not isinstance(lang, dict):
                        continue
                    raw_display = lang.get("display_tags")
                    if isinstance(raw_display, list) and len(raw_display) > 0:
                        cand = []
                        for t in raw_display:
                            if not isinstance(t, str):
                                continue
                            trimmed = _go_trim_space(t)
                            if trimmed:
                                cand.append(trimmed)
                    else:
                        filenames = [fn for fn in (lang.get("filenames") or []) if isinstance(fn, str) and fn]
                        if filenames:
                            cand = filenames
                        else:
                            cand = [ext for ext in (lang.get("extensions") or []) if isinstance(ext, str) and ext]
                    for tag in cand:
                        low = _go_lower(tag)
                        if low in seen_tags:
                            continue
                        seen_tags.add(low)
                        tags.append(tag)
                        if len(tags) >= 6:
                            break
                    if len(tags) >= 6:
                        break
                if tags:
                    app_entry["file_extensions"] = tags
            if m.get("beta") is True:
                app_entry["beta"] = True
            if releases:
                inline = [normalize_release_entry(r, include_body=False) for r in releases[:MAX_CHANGELOG_INLINE]]
                inline = [item for item in inline if item]
                if inline:
                    app_entry["changelog_url"] = f"{CHANGELOG_BASE_URL}/{pid}.json"
                    app_entry["changelog"] = inline
                app_entry["_full_releases"] = releases[:MAX_CHANGELOG_FULL]
            apps.append(app_entry)
        except Exception as exc:
            print(f"[WARN] skip {full}: {exc}")

    apps = [app for app in apps if app["id"].lower() not in ambiguous]
    validate_apps_against_verified(apps, id_to_repo)
    apps.sort(key=lambda x: (not x["verified"], -x.get("downloads", 0), -x.get("stars", 0), x["id"]))
    return {"app": "blockmd", "schema_version": 1, "topic": "blockmd-plugin", "updated_at": now_iso, "apps": apps}


def fetch_github_repo(repo, now_iso, release_tag=None):
    full = repo["full_name"]
    dl_url, gh_downloads, updated = "", 0, repo.get("pushed_at") or now_iso
    latest = None
    stable_releases = []
    fetched_any = False
    hit_page_cap = False
    max_pages = 5
    if release_tag:
        latest = json.loads(gh_api_get(f"{API}/repos/{full}/releases/tags/{release_tag}"))
        if not isinstance(latest, dict) or latest.get("draft") or latest.get("prerelease") or latest.get("tag_name") != release_tag:
            raise ValueError(f"invalid compatibility release: {full}@{release_tag}")
        stable_releases.append(latest)
    for page in range(1, max_pages + 1) if release_tag is None else ():
        try:
            rels = json.loads(gh_api_get(f"{API}/repos/{full}/releases?per_page=100&page={page}"))
        except Exception:
            break
        if not isinstance(rels, list):
            break
        fetched_any = True
        for r in rels:
            for ast in r.get("assets", []):
                gh_downloads += int(ast.get("download_count") or 0)
            if not r.get("draft") and not r.get("prerelease"):
                if latest is None:
                    latest = r
                if len(stable_releases) < MAX_CHANGELOG_FULL:
                    stable_releases.append(r)
        if len(rels) < 100:
            break
        if page == max_pages:
            hit_page_cap = True
    if latest is None and (hit_page_cap or not fetched_any):
        try:
            rel = json.loads(gh_api_get(f"{API}/repos/{full}/releases/latest"))
            if isinstance(rel, dict) and not rel.get("draft") and not rel.get("prerelease"):
                latest = rel
                stable_releases.append(rel)
        except Exception:
            pass
    if latest is not None:
        updated = latest.get("published_at") or updated
        for ast in latest.get("assets", []):
            if str(ast.get("name") or "").lower().endswith((".tar.gz", ".tgz", ".zip")):
                dl_url = ast["browser_download_url"]
                break
        if not dl_url:
            dl_url = latest.get("tarball_url") or ""
    if not dl_url:
        branch = repo.get("default_branch") or "main"
        dl_url = f"https://codeload.github.com/{full}/tar.gz/{branch}"
    raw = download_archive(dl_url)
    sha_hex = hashlib.sha256(raw).hexdigest()
    m, docs = extract_manifest_and_docs(raw, dl_url)
    return dl_url, sha_hex, gh_downloads, updated, m, docs, stable_releases


def add_system_apps(index, repositories, blocklist, now_iso):
    legacy, system = [], []
    for entry in index["apps"]:
        if not any(p.startswith("system:") or p == "events:system" for p in entry["permissions"]):
            legacy.append(entry)
            continue
        system.append(entry)
        repo = entry["repo"].lower()
        tag = LEGACY_RELEASES.get(repo)
        if not tag:
            continue
        plugin_id = entry["id"]
        prior = build_index(
            [repositories[repo]], {plugin_id: entry["downloads"]}, blocklist,
            {plugin_id: repo}, {repo: plugin_id},
            lambda source, timestamp: fetch_github_repo(source, timestamp, tag), now_iso,
            require_official_category=False,
        )["apps"][0]
        if prior["version"] != tag.removeprefix("v") or any(
            p.startswith("system:") or p == "events:system" for p in prior["permissions"]
        ):
            raise ValueError(f"incompatible legacy release: {repo}@{tag}")
        legacy.append(prior)
    index["apps"] = legacy
    if system:
        index["system_apps"] = system


def write_changelog_files(index, now_iso, out_dir="changelogs"):
    by_id = {}
    for bucket in (index.get("apps", []), index.get("system_apps", [])):
        for entry in bucket:
            raw_releases = entry.pop("_full_releases", None)
            if not raw_releases:
                continue
            pid = entry["id"]
            if pid in by_id and len(by_id[pid]["releases"]) >= len(raw_releases):
                continue
            normalized = [normalize_release_entry(r, include_body=True) for r in raw_releases[:MAX_CHANGELOG_FULL]]
            normalized = [item for item in normalized if item]
            if not normalized:
                continue
            by_id[pid] = {
                "app": "blockmd",
                "schema_version": 1,
                "id": pid,
                "repo": entry.get("repo", ""),
                "updated_at": now_iso,
                "latest_version": normalized[0]["version"],
                "releases": normalized,
            }
    os.makedirs(out_dir, exist_ok=True)
    written = set()
    for pid, doc in sorted(by_id.items()):
        filename = f"{pid}.json"
        written.add(filename)
        path = os.path.join(out_dir, filename)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    for existing in os.listdir(out_dir):
        if existing.endswith(".json") and existing not in written:
            os.remove(os.path.join(out_dir, existing))


def main():
    blocklist = load_set("blocklist.json")
    id_to_repo, repo_to_id = load_verified_map("verified.json")
    prev_dl = {}
    if os.path.exists("store-index.json"):
        with open("store-index.json", encoding="utf-8") as previous:
            previous_index = json.load(previous)
        for a in previous_index.get("apps", []) + previous_index.get("system_apps", []):
            prev_dl[a.get("id")] = max(prev_dl.get(a.get("id"), 0), int(a.get("downloads") or 0))

    search = json.loads(gh_api_get(f"{API}/search/repositories?q=topic:blockmd-plugin+fork:false+archived:false&sort=stars&order=desc&per_page=100"))
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repositories = search.get("items", [])
    out = build_index(
        repositories,
        prev_dl,
        blocklist,
        id_to_repo,
        repo_to_id,
        fetch_github_repo,
        now_iso,
    )
    add_system_apps(out, {repo["full_name"].lower(): repo for repo in repositories}, blocklist, now_iso)
    write_changelog_files(out, now_iso)
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
