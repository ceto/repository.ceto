#!/usr/bin/env python3
"""Tooling for the repository.ceto Kodi add-on repository (standard library only).

Published layout (one directory tree, served as-is by GitHub Pages):

    addons.xml                       index of the newest version of every add-on
    addons.xml.md5                   md5 (hex, no newline) of the exact addons.xml bytes
    index.html                       landing page; also browsable by Kodi's file manager
    <id>/index.html                  Kodi-browsable list of the add-on's ZIPs
    <id>/<id>-<version>.zip          every published version, never overwritten
    <id>/<asset path>                icon / fanart declared in addon.xml (convenience)

Commands:

    validate --source DIR --id ID [--tag TAG]
    package  --source DIR --id ID --out FILE
    stage    --source DIR --id ID --site DIR [--tag TAG]
    index    --site DIR
    verify   --site DIR | --url BASE_URL
    source-info --id ID [--config addons.json]
"""
import argparse
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "addons.json")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
XML_DECL_RE = re.compile(r"^﻿?\s*<\?xml[^>]*\?>\s*")

FIXED_DATE = (2026, 1, 1, 0, 0, 0)
INCLUDE_FILES = ("addon.xml", "addon.py", "LICENSE", "LICENSE.txt", "LICENSE.md",
                 "changelog.txt", "icon.png", "fanart.png")
INCLUDE_DIRS = ("resources",)
SKIP_DIRS = {"__pycache__", ".git", ".github"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = (".pyc", ".pyo")
# Never packaged; their presence is an error, not something to skip silently.
DENY_NAMES = {"_local_key.py", ".env", "id_rsa", "id_ed25519"}
DENY_PREFIXES = (".env.",)
DENY_SUFFIXES = (".har", ".pem", ".key", ".p12", ".pfx")
DENY_SUBSTRINGS = (".secret",)
ASSET_TAGS = ("icon", "fanart", "banner", "clearlogo", "screenshot")


class RepoError(Exception):
    pass


class ValidationError(RepoError):
    pass


class PackageError(RepoError):
    pass


class ConflictError(RepoError):
    pass


# --------------------------------------------------------------------- versions

def parse_version(text):
    """Kodi-style numeric version (2 to 4 dot-separated integers) as a padded tuple."""
    if not isinstance(text, str) or not VERSION_RE.match(text):
        raise ValidationError("invalid version %r: use 2 to 4 numeric parts such as 1.2.3" % (text,))
    parts = [int(p) for p in text.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


# ------------------------------------------------------------------- addon.xml

class AddonInfo:
    def __init__(self, addon_id, version, libraries, assets, warnings):
        self.id = addon_id
        self.version = version
        self.libraries = libraries
        self.assets = assets
        self.warnings = warnings


def _text(node):
    return (node.text or "").strip() if node is not None else ""


def validate_addon_xml(data, expected_id, tag=None):
    """Validate addon.xml bytes. Raises ValidationError listing every problem found."""
    errors, warnings = [], []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValidationError("addon.xml is not well-formed XML: %s" % exc)
    if root.tag != "addon":
        raise ValidationError("addon.xml root element must be <addon>, found <%s>" % root.tag)

    addon_id = root.get("id", "")
    version = root.get("version", "")
    if not ID_RE.match(addon_id):
        errors.append("invalid add-on id %r" % addon_id)
    if addon_id != expected_id:
        errors.append("add-on id %r does not match the expected id %r" % (addon_id, expected_id))
    try:
        parse_version(version)
    except ValidationError as exc:
        errors.append(str(exc))
    for attr in ("name", "provider-name"):
        if not root.get(attr, "").strip():
            errors.append("missing %s attribute" % attr)
    if tag is not None and version and tag.lstrip("v") != version:
        errors.append("release tag %r does not match addon.xml version %r" % (tag, version))

    extensions = root.findall("extension")
    libraries = []
    for ext in extensions:
        lib = ext.get("library")
        if lib:
            libraries.append(lib)
    if any(e.get("point") == "xbmc.python.pluginsource" for e in extensions):
        imports = [i.get("addon") for i in root.findall("./requires/import")]
        if "xbmc.python" not in imports:
            errors.append("plugin add-on must import xbmc.python")
        if not libraries:
            errors.append("pluginsource extension has no library attribute")

    meta = [e for e in extensions if e.get("point") == "xbmc.addon.metadata"]
    assets = []
    if len(meta) != 1:
        errors.append("exactly one xbmc.addon.metadata extension is required")
    else:
        m = meta[0]
        if not _text(m.find("license")):
            errors.append("metadata is missing <license>")
        if not m.findall("summary"):
            errors.append("metadata is missing <summary>")
        if not m.findall("description"):
            errors.append("metadata is missing <description>")
        notice = " ".join(_text(n) for tag_name in ("summary", "description", "disclaimer")
                          for n in m.findall(tag_name)).lower()
        if "unofficial" not in notice and "nem hivatalos" not in notice:
            errors.append("summary/description/disclaimer must state that the add-on is unofficial")
        for name in ("source", "website", "email"):
            if not _text(m.find(name)):
                warnings.append("metadata has no <%s>" % name)
        for kind in ASSET_TAGS:
            for node in m.findall("./assets/" + kind):
                path = _text(node)
                if path:
                    assets.append(path)
        if not any(_text(n) for n in m.findall("./assets/icon")):
            warnings.append("no <icon> asset declared")

    if errors:
        raise ValidationError("; ".join(errors))
    return AddonInfo(addon_id, version, libraries, assets, warnings)


def _safe_rel(path):
    norm = os.path.normpath(path)
    if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        raise ValidationError("path %r escapes the add-on directory" % path)
    return norm.replace(os.sep, "/")


def load_source(source, addon_id, tag=None):
    path = os.path.join(source, "addon.xml")
    if not os.path.isfile(path):
        raise ValidationError("%s has no addon.xml" % source)
    with open(path, "rb") as fh:
        data = fh.read()
    info = validate_addon_xml(data, addon_id, tag)
    for rel in info.libraries + info.assets:
        rel = _safe_rel(rel)
        if not os.path.isfile(os.path.join(source, rel)):
            raise ValidationError("addon.xml references %r but the file is missing" % rel)
    return info, data


# -------------------------------------------------------------------- packaging

def _denied(name):
    return (name in DENY_NAMES or name.startswith(DENY_PREFIXES) or name.endswith(DENY_SUFFIXES)
            or any(s in name for s in DENY_SUBSTRINGS))


def collect_files(source, extra=()):
    """Sorted, relative, forward-slash paths that go into the ZIP."""
    files = set()
    for name in tuple(INCLUDE_FILES) + tuple(_safe_rel(e) for e in extra):
        path = os.path.join(source, name)
        if os.path.islink(path):
            raise PackageError("symbolic link not allowed: %s" % name)
        if os.path.isfile(path):
            files.add(name)
    for entry in INCLUDE_DIRS:
        top = os.path.join(source, entry)
        if os.path.islink(top):
            raise PackageError("symbolic link not allowed: %s" % entry)
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for d in dirnames:
                if os.path.islink(os.path.join(dirpath, d)):
                    raise PackageError("symbolic link not allowed: %s" % os.path.join(dirpath, d))
            for name in filenames:
                full = os.path.join(dirpath, name)
                if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                    continue
                if os.path.islink(full):
                    raise PackageError("symbolic link not allowed: %s" % os.path.relpath(full, source))
                files.add(os.path.relpath(full, source).replace(os.sep, "/"))
    for rel in files:
        if _denied(os.path.basename(rel)):
            raise PackageError("refusing to package %s: file name looks like a secret or local-only file" % rel)
    if "addon.xml" not in files:
        raise PackageError("addon.xml missing from the package")
    return sorted(files)


def build_zip(source, info, out_path):
    """Deterministic ZIP: single top-level <id>/ folder, sorted entries, fixed timestamps."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in collect_files(source, info.libraries):
            with open(os.path.join(source, rel), "rb") as fh:
                data = fh.read()
            zi = zipfile.ZipInfo("%s/%s" % (info.id, rel), FIXED_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data, compresslevel=9)
    os.replace(tmp, out_path)
    return out_path


def zip_name(addon_id, version):
    return "%s-%s.zip" % (addon_id, version)


def inspect_zip(fileobj, addon_id=None):
    """Check a package's layout; return (id, version, addon.xml bytes)."""
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as exc:
        raise PackageError("not a valid ZIP: %s" % exc)
    names = zf.namelist()
    if not names:
        raise PackageError("empty ZIP")
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) != 1:
        raise PackageError("ZIP must have exactly one top-level folder, found %s" % sorted(tops))
    top = tops.pop()
    if addon_id is not None and top != addon_id:
        raise PackageError("top-level folder %r does not match add-on id %r" % (top, addon_id))
    if len(set(names)) != len(names):
        raise PackageError("duplicate entries in ZIP")
    for n in names:
        if n.startswith("/") or ".." in n.split("/"):
            raise PackageError("unsafe path in ZIP: %r" % n)
    key = top + "/addon.xml"
    if key not in names:
        raise PackageError("ZIP has no %s" % key)
    data = zf.read(key)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise PackageError("addon.xml inside the ZIP is not well-formed: %s" % exc)
    if root.get("id") != top:
        raise PackageError("addon.xml id %r does not match the folder %r" % (root.get("id"), top))
    return top, root.get("version", ""), data


# ------------------------------------------------------------------- site files

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def published_versions(site, addon_id):
    """{version string: path} of every ZIP published for the id."""
    found = {}
    folder = os.path.join(site, addon_id)
    if not os.path.isdir(folder):
        return found
    pattern = re.compile(r"^%s-(\d+(?:\.\d+){1,3})\.zip$" % re.escape(addon_id))
    for name in os.listdir(folder):
        m = pattern.match(name)
        if m:
            found[m.group(1)] = os.path.join(folder, name)
    return found


def site_addon_ids(site):
    ids = []
    if os.path.isdir(site):
        for name in sorted(os.listdir(site)):
            if not name.startswith(".") and os.path.isdir(os.path.join(site, name)) \
                    and published_versions(site, name):
                ids.append(name)
    return ids


def newest(versions):
    return max(versions, key=parse_version)


def generate_index(site):
    """Rewrite addons.xml, addons.xml.md5, index.html and <id>/index.html from the ZIPs in site/."""
    ids = site_addon_ids(site)
    if not ids:
        raise RepoError("no add-on ZIPs found in %s; refusing to write an empty index" % site)
    entries = []
    repo_zip = None
    for addon_id in ids:
        versions = published_versions(site, addon_id)
        version = newest(versions)
        with open(versions[version], "rb") as fh:
            zid, zver, data = inspect_zip(fh, addon_id)
        if zver != version:
            raise RepoError("%s: file name says %s but addon.xml says %s" % (versions[version], version, zver))
        text = XML_DECL_RE.sub("", data.decode("utf-8")).strip()
        entries.append(text)
        if addon_id == "repository.ceto":
            repo_zip = "%s/%s" % (addon_id, zip_name(addon_id, version))
    body = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
            + "\n".join(entries) + "\n</addons>\n").encode("utf-8")
    ET.fromstring(body)  # must parse
    _write(os.path.join(site, "addons.xml"), body)
    _write(os.path.join(site, "addons.xml.md5"), hashlib.md5(body).hexdigest().encode("ascii"))
    # Kodi's HTTP directory parser (CHTTPDirectory) lists a link only when its text equals its
    # href (ignoring a trailing slash) and href is the first attribute; it must be followed by
    # more text on the same line. So the root links each add-on folder as "<id>/", and every
    # folder has its own index.html listing its ZIPs: add the site as a source in Kodi's file
    # manager and "Install from zip file" can browse it.
    rows = "".join('<li><a href="%s/">%s/</a> %s</li>\n'
                   % (html.escape(i), html.escape(i), html.escape(newest(published_versions(site, i))))
                   for i in ids)
    link = ('<p>Download: <a href="%s">%s</a></p>\n'
            % (html.escape(repo_zip), html.escape(repo_zip.split("/")[-1]))) if repo_zip else ""
    page = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>Ceto Kodi add-on repository (unofficial)</title></head><body>\n"
            "<h1>Ceto Kodi add-on repository</h1>\n"
            "<p>Unofficial community add-ons, not affiliated with any content provider. "
            "Install the repository ZIP in Kodi 21 or newer "
            "(Settings &rarr; Add-ons &rarr; Install from zip file).</p>\n" + link +
            "<p>Or straight from Kodi: add this address as a source in the File manager, then "
            "Install from zip file &rarr; that source &rarr; repository.ceto &rarr; the ZIP.</p>\n"
            "<ul>\n" + rows + "</ul>\n</body></html>\n")
    for addon_id in ids:
        names = sorted((zip_name(addon_id, v) for v in published_versions(site, addon_id)),
                       key=lambda n: parse_version(n[len(addon_id) + 1:-4]), reverse=True)
        items = "".join('<li><a href="%s">%s</a></li>\n' % (html.escape(n), html.escape(n)) for n in names)
        folder_page = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                       "<title>%s</title></head><body>\n<h1>%s</h1>\n<ul>\n"
                       '<li><a href="../">Parent directory</a></li>\n%s</ul>\n</body></html>\n'
                       % (html.escape(addon_id), html.escape(addon_id), items))
        _write(os.path.join(site, addon_id, "index.html"), folder_page.encode("utf-8"))
    _write(os.path.join(site, "index.html"), page.encode("utf-8"))
    return ids


def _write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _canon(data):
    return ET.canonicalize(xml_data=data)


def _verify_index(index_bytes, md5_text, fetch_zip):
    """Shared checks. fetch_zip(id, version) -> bytes or None."""
    problems = []
    if hashlib.md5(index_bytes).hexdigest() != md5_text.strip():
        problems.append("addons.xml.md5 does not match addons.xml")
    try:
        root = ET.fromstring(index_bytes)
    except ET.ParseError as exc:
        return problems + ["addons.xml is not well-formed: %s" % exc]
    if root.tag != "addons" or not len(root):
        return problems + ["addons.xml must be a non-empty <addons> element"]
    seen = set()
    for node in root:
        aid, ver = node.get("id"), node.get("version")
        label = "%s %s" % (aid, ver)
        if aid in seen:
            problems.append("%s listed twice" % aid)
        seen.add(aid)
        blob = fetch_zip(aid, ver)
        if blob is None:
            problems.append("%s: ZIP is not available" % label)
            continue
        try:
            zid, zver, data = inspect_zip(io.BytesIO(blob), aid)
        except RepoError as exc:
            problems.append("%s: %s" % (label, exc))
            continue
        if zver != ver:
            problems.append("%s: ZIP contains version %s" % (label, zver))
        elif _canon(data) != _canon(ET.tostring(node)):
            problems.append("%s: addons.xml entry differs from the addon.xml inside the ZIP" % label)
    return problems


def verify_site(site):
    problems = []
    for name in ("addons.xml", "addons.xml.md5"):
        if not os.path.isfile(os.path.join(site, name)):
            problems.append("missing %s" % name)
    if problems:
        return problems
    with open(os.path.join(site, "addons.xml"), "rb") as fh:
        index = fh.read()
    with open(os.path.join(site, "addons.xml.md5"), "r", encoding="ascii") as fh:
        md5 = fh.read()

    def fetch(aid, ver):
        path = os.path.join(site, aid, zip_name(aid, ver))
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    problems += _verify_index(index, md5, fetch)
    listed = {}
    for node in ET.fromstring(index):
        listed[node.get("id")] = node.get("version")
    for aid in site_addon_ids(site):
        versions = published_versions(site, aid)
        for ver, path in versions.items():
            try:
                with open(path, "rb") as fh:
                    _, zver, _ = inspect_zip(fh, aid)
                if zver != ver:
                    problems.append("%s: file name says %s but addon.xml says %s" % (path, ver, zver))
            except RepoError as exc:
                problems.append("%s: %s" % (path, exc))
        if listed.get(aid) != newest(versions):
            problems.append("%s: addons.xml lists %s but the newest ZIP is %s"
                            % (aid, listed.get(aid), newest(versions)))
    return problems


def verify_url(base, attempts=1, delay=10, opener=urllib.request.urlopen):
    base = base.rstrip("/")

    def get(url):
        try:
            with opener(url, timeout=30) as resp:
                return resp.read()
        except Exception:
            return None

    problems = []
    for attempt in range(attempts):
        index = get(base + "/addons.xml")
        md5 = get(base + "/addons.xml.md5")
        if index is None or md5 is None:
            problems = ["addons.xml or addons.xml.md5 is not reachable at %s" % base]
        else:
            problems = _verify_index(index, md5.decode("ascii", "replace"),
                                     lambda a, v: get("%s/%s/%s" % (base, a, zip_name(a, v))))
        if not problems:
            return []
        if attempt + 1 < attempts:
            time.sleep(delay)
    return problems


# ------------------------------------------------------------------------ stage

def _differs(a, b):
    return not os.path.isfile(b) or _sha256(a) != _sha256(b)


def stage(source, addon_id, site, tag=None):
    """Validate + package + merge into site. All-or-nothing: site is only touched once the
    complete candidate tree has been generated and verified in a scratch copy."""
    info, _ = load_source(source, addon_id, tag)
    work = tempfile.mkdtemp(prefix="kodirepo-")
    try:
        cand = os.path.join(work, "site")
        if os.path.isdir(site):
            shutil.copytree(site, cand)
        else:
            os.makedirs(cand)
        new_zip = os.path.join(work, zip_name(info.id, info.version))
        build_zip(source, info, new_zip)

        want = parse_version(info.version)
        existing = published_versions(cand, info.id)
        status = "added"
        for ver, path in existing.items():
            if parse_version(ver) == want:
                if ver == info.version and _sha256(path) == _sha256(new_zip):
                    status = "unchanged"
                else:
                    raise ConflictError(
                        "%s %s is already published with different content; bump the version instead "
                        "of overwriting it" % (info.id, ver))
        if status == "added" and existing and want < parse_version(newest(existing)):
            raise ConflictError("version %s of %s is not newer than the published %s"
                                % (info.version, info.id, newest(existing)))
        if status == "added":
            os.makedirs(os.path.join(cand, info.id), exist_ok=True)
            shutil.copyfile(new_zip, os.path.join(cand, info.id, zip_name(info.id, info.version)))
        for rel in info.assets:
            rel = _safe_rel(rel)
            dest = os.path.join(cand, info.id, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(source, rel), dest)
        generate_index(cand)
        problems = verify_site(cand)
        if problems:
            raise RepoError("candidate site failed verification: " + "; ".join(problems))

        changed = []
        for dirpath, _, filenames in os.walk(cand):
            for name in filenames:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, cand)
                if _differs(src, os.path.join(site, rel)):
                    changed.append(rel)
        order = {"index.html": 1, "addons.xml": 2, "addons.xml.md5": 3}
        for rel in sorted(changed, key=lambda r: (order.get(r, 0), r)):
            with open(os.path.join(cand, rel), "rb") as fh:
                _write(os.path.join(site, rel), fh.read())
        return {"id": info.id, "version": info.version, "status": status,
                "zip": "%s/%s" % (info.id, zip_name(info.id, info.version)),
                "sha256": _sha256(new_zip), "changed": sorted(changed), "warnings": info.warnings}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------- config

def source_info(addon_id, config=DEFAULT_CONFIG):
    with open(config, encoding="utf-8") as fh:
        cfg = json.load(fh)["addons"]
    if addon_id not in cfg:
        raise RepoError("%r is not an add-on this repository publishes (see addons.json)" % addon_id)
    entry = cfg[addon_id]
    return {"id": addon_id, "repo": entry.get("repo", ""), "path": entry.get("path", "")}


# -------------------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "package", "stage"):
        s = sub.add_parser(name)
        s.add_argument("--source", required=True)
        s.add_argument("--id", required=True)
        if name != "package":
            s.add_argument("--tag")
        if name == "package":
            s.add_argument("--out", required=True)
        if name == "stage":
            s.add_argument("--site", required=True)
    s = sub.add_parser("index")
    s.add_argument("--site", required=True)
    s = sub.add_parser("verify")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--site")
    g.add_argument("--url")
    s.add_argument("--attempts", type=int, default=1)
    s = sub.add_parser("source-info")
    s.add_argument("--id", required=True)
    s.add_argument("--config", default=DEFAULT_CONFIG)
    args = p.parse_args(argv)

    try:
        if args.cmd == "validate":
            info, _ = load_source(args.source, args.id, args.tag)
            print(json.dumps({"id": info.id, "version": info.version, "warnings": info.warnings}))
        elif args.cmd == "package":
            info, _ = load_source(args.source, args.id)
            print(build_zip(args.source, info, args.out))
        elif args.cmd == "stage":
            print(json.dumps(stage(args.source, args.id, args.site, args.tag), indent=2))
        elif args.cmd == "index":
            print(json.dumps(generate_index(args.site)))
        elif args.cmd == "verify":
            problems = verify_site(args.site) if args.site else verify_url(args.url, args.attempts)
            if problems:
                for item in problems:
                    sys.stderr.write("verify: %s\n" % item)
                return 1
            print("ok")
        elif args.cmd == "source-info":
            print(json.dumps(source_info(args.id, args.config)))
    except RepoError as exc:
        sys.stderr.write("kodirepo: %s\n" % exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
