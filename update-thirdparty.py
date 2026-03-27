#!/usr/bin/env python3
"""
Update thirdparty JAR dependencies in org.scilab.Scilab.yaml when Scilab
releases a new version.

Automatically updates all Maven Central JARs by:
  1. Fetching the new Scilab version's ivy.xml from GitLab
  2. Comparing artifact versions against what the manifest currently has
  3. Fetching new SHA256s from Maven Central (or downloading + hashing)
  4. Patching the YAML in-place

Non-Maven JARs (JOGL, JCEF, JavaFX, jgraphx, flexdock, jrosetta, BWidget)
are reported for manual review since they require version-specific handling.

Usage:
    python update-thirdparty.py 2026.1.0
    python update-thirdparty.py 2026.1.0 --dry-run
    python update-thirdparty.py 2026.1.0 --scilab-sha256 <sha256-of-source-tarball>
"""

import sys
import re
import urllib.request
import urllib.error
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
MANIFEST = SCRIPT_DIR / "org.scilab.Scilab.yaml"

# ivy.xml path in Scilab's GitLab source tree
IVY_XML_URL = (
    "https://gitlab.com/scilab/scilab/-/raw/{version}/scilab"
    "/modules/prebuildjava/ivy.xml"
)

# Scilab source tarball URL template
SCILAB_SOURCE_URL = (
    "https://gitlab.com/scilab/scilab/-/archive/{version}"
    "/scilab-{version}.tar.bz2"
)

# Maven Central base
MAVEN_BASE = "https://repo1.maven.org/maven2"

# Maven URL pattern: group_path/artifact/version/artifact-version.jar
# Also handles classifier suffixes like javafx-base-17.0.13-linux.jar
MAVEN_URL_RE = re.compile(
    r"(https://repo1\.maven\.org/maven2/)"
    r"([^/]+(?:/[^/]+)*)"   # group path  (group1 = base, group2 = path)
    r"/([^/]+)"              # artifact    (group3)
    r"/([^/]+)"              # version     (group4)
    r"/\3-\4(-[^/]+)?\.jar" # artifact-version[-classifier].jar (group5 = classifier)
)


# ---------------------------------------------------------------------------
# ivy.xml helpers
# ---------------------------------------------------------------------------

def fetch_ivy_xml(scilab_version: str) -> bytes:
    url = IVY_XML_URL.format(version=scilab_version)
    print(f"Fetching ivy.xml: {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"Error fetching ivy.xml (HTTP {e.code}): {url}\n"
                 f"Check that version '{scilab_version}' exists as a GitLab tag.")


def parse_ivy_xml(content: bytes) -> dict[tuple[str, str], str]:
    """Return {(org, artifact): version} for all <dependency> elements."""
    root = ET.fromstring(content)
    result = {}
    for dep in root.iter("dependency"):
        org  = dep.get("org")
        name = dep.get("name")
        rev  = dep.get("rev")
        if org and name and rev:
            result[(org, name)] = rev
    return result


# ---------------------------------------------------------------------------
# Maven Central SHA256
# ---------------------------------------------------------------------------

def maven_sha256(url: str) -> str:
    """
    Fetch SHA256 for a Maven Central artifact.
    Tries the .sha256 sidecar file first; falls back to downloading the JAR.
    """
    sha256_url = url + ".sha256"
    try:
        with urllib.request.urlopen(sha256_url, timeout=30) as r:
            digest = r.read().decode().strip().split()[0]
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    except Exception:
        pass  # fall through to download

    print(f"    (no .sha256 sidecar — downloading JAR to hash)")
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r:
        while chunk := r.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# URL parsing / building
# ---------------------------------------------------------------------------

def parse_maven_url(url: str) -> Optional[tuple[str, str, str, str]]:
    """
    Returns (group_path, artifact, version, classifier_suffix) or None.
    classifier_suffix is e.g. '-linux' for javafx-base-17.0.13-linux.jar, else ''.
    """
    m = MAVEN_URL_RE.match(url)
    if not m:
        return None
    group_path = m.group(2)
    artifact   = m.group(3)
    version    = m.group(4)
    classifier = m.group(5) or ""
    return group_path, artifact, version, classifier


def build_maven_url(group_path: str, artifact: str, version: str, classifier: str = "") -> str:
    return (f"{MAVEN_BASE}/{group_path}/{artifact}/{version}"
            f"/{artifact}-{version}{classifier}.jar")


def group_path_to_org(path: str) -> str:
    return path.replace("/", ".")


# ---------------------------------------------------------------------------
# YAML text patch helpers
# ---------------------------------------------------------------------------

def find_all_maven_urls(text: str) -> list[tuple[int, str]]:
    """Return list of (char_offset, url) for every Maven Central JAR URL in text."""
    results = []
    for m in re.finditer(
        r"url:\s+(https://repo1\.maven\.org/maven2/[^\s]+\.jar)", text
    ):
        results.append((m.start(1), m.group(1)))
    return results


def patch_url_and_sha256(text: str, old_url: str, new_url: str, new_sha256: str) -> str:
    """
    Replace the URL and its immediately following sha256 line.
    Also replaces any dest-filename that embeds the old version number.
    """
    # Find url: <old_url>
    url_pos = text.find(f"url: {old_url}")
    if url_pos == -1:
        return text  # already updated or not found

    # Replace URL
    text = text.replace(f"url: {old_url}", f"url: {new_url}", 1)

    # Find sha256 on the line immediately after the url line
    sha_re = re.compile(r"(sha256:\s+)([0-9a-f]{64})")
    # Search in a small window after the url
    window_start = url_pos
    window_end   = window_start + 300
    window       = text[window_start:window_end]
    sha_m = sha_re.search(window)
    if sha_m:
        old_sha_block = sha_m.group(0)
        new_sha_block = sha_m.group(1) + new_sha256
        text = text[:window_start] + window.replace(old_sha_block, new_sha_block, 1) + text[window_end:]

    return text


def patch_dest_filename(text: str, artifact: str, old_version: str, new_version: str) -> str:
    """
    If a dest-filename: line near the artifact URL contains old_version, update it.
    Example: lucene-analyzers-common-9.10.0.jar → lucene-analyzers-common-9.11.0.jar
    """
    # Replace any occurrence of -{old_version}.jar that follows the artifact name
    # (conservative: only matches filename-like strings)
    old_pat = f"-{old_version}.jar"
    new_pat = f"-{new_version}.jar"
    # Only replace in dest-filename lines
    def replace_in_dest_filename(m):
        return m.group(0).replace(old_pat, new_pat)
    text = re.sub(
        r"(dest-filename:[^\n]*)" + re.escape(old_pat),
        replace_in_dest_filename,
        text,
    )
    return text


# ---------------------------------------------------------------------------
# Scilab source entry update
# ---------------------------------------------------------------------------

def update_scilab_source_entry(text: str, new_version: str,
                                new_sha256: Optional[str]) -> str:
    """Update the Scilab source archive URL and sha256."""
    old_url_re = re.compile(
        r"(url:\s+https://gitlab\.com/scilab/scilab/-/archive/)"
        r"([^/]+)"
        r"(/scilab-)"
        r"\2"
        r"(\.tar\.bz2)"
    )
    m = old_url_re.search(text)
    if not m:
        print("  WARNING: could not find Scilab source URL in manifest.")
        return text

    old_url = m.group(0).split("url: ", 1)[1] if "url: " in text[m.start()-6:m.end()] else m.group(0)
    # Reconstruct properly
    old_full_line = f"url: {m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}{m.group(4)}"
    new_full_line = f"url: {m.group(1)}{new_version}{m.group(3)}{new_version}{m.group(4)}"

    if old_full_line == new_full_line:
        print("  Scilab source URL already at correct version.")
        return text

    text = text.replace(old_full_line, new_full_line, 1)
    print(f"  Updated Scilab source URL → scilab-{new_version}.tar.bz2")

    if new_sha256:
        # Replace the sha256 that follows this URL
        pos = text.find(new_full_line)
        if pos != -1:
            window = text[pos:pos+200]
            sha_m = re.search(r"(sha256:\s+)([0-9a-f]{64})", window)
            if sha_m:
                text = (text[:pos]
                        + window.replace(sha_m.group(0),
                                         sha_m.group(1) + new_sha256, 1)
                        + text[pos+200:])
                print(f"  Updated Scilab source sha256.")
    else:
        print("  NOTE: Scilab source sha256 NOT updated. Pass --scilab-sha256 <hash> to update it.")

    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    scilab_sha256 = None
    if "--scilab-sha256" in args:
        idx = args.index("--scilab-sha256")
        scilab_sha256 = args[idx + 1]
        args = args[:idx] + args[idx+2:]

    if not args:
        sys.exit("Error: missing Scilab version argument.")
    new_scilab_version = args[0]

    if not MANIFEST.exists():
        sys.exit(f"Manifest not found: {MANIFEST}")

    # ── Fetch ivy.xml ────────────────────────────────────────────────────────
    ivy_content = fetch_ivy_xml(new_scilab_version)
    ivy_versions = parse_ivy_xml(ivy_content)
    print(f"  {len(ivy_versions)} dependencies parsed from ivy.xml\n")

    # ── Read manifest ────────────────────────────────────────────────────────
    manifest_text = MANIFEST.read_text()

    # ── Update Scilab source entry ───────────────────────────────────────────
    print("── Scilab source tarball ────────────────────────────────────────────")
    manifest_text = update_scilab_source_entry(manifest_text, new_scilab_version, scilab_sha256)
    print()

    # ── Update Maven Central JARs ────────────────────────────────────────────
    print("── Maven Central JARs ───────────────────────────────────────────────")

    maven_urls = find_all_maven_urls(manifest_text)
    updated_count = 0
    already_current = 0
    not_in_ivy = []
    errors = []

    for _, current_url in maven_urls:
        parsed = parse_maven_url(current_url)
        if not parsed:
            continue
        group_path, artifact, current_version, classifier = parsed
        org = group_path_to_org(group_path)

        # Look up new version from ivy.xml
        new_version = ivy_versions.get((org, artifact))

        # Some ivy entries use a different org; try by artifact name alone
        if new_version is None:
            matches = [(o, a, v) for (o, a), v in ivy_versions.items() if a == artifact]
            if len(matches) == 1:
                new_version = matches[0][2]

        if new_version is None:
            not_in_ivy.append((artifact, current_version, current_url))
            continue

        if new_version == current_version:
            already_current += 1
            continue

        new_url = build_maven_url(group_path, artifact, new_version, classifier)
        print(f"  {artifact}: {current_version} → {new_version}")

        try:
            new_sha256 = maven_sha256(new_url)
            manifest_text = patch_url_and_sha256(
                manifest_text, current_url, new_url, new_sha256
            )
            manifest_text = patch_dest_filename(
                manifest_text, artifact, current_version, new_version
            )
            updated_count += 1
            print(f"    sha256: {new_sha256}")
        except Exception as e:
            print(f"    ERROR: {e}")
            errors.append((artifact, str(e)))

    print(f"\n  Updated : {updated_count}")
    print(f"  Current : {already_current}")
    if not_in_ivy:
        print(f"\n  Not found in ivy.xml (kept as-is):")
        for artifact, version, url in not_in_ivy:
            print(f"    {artifact} {version}")
            print(f"      {url}")
    if errors:
        print(f"\n  Errors:")
        for artifact, err in errors:
            print(f"    {artifact}: {err}")

    # ── Write manifest ───────────────────────────────────────────────────────
    if not dry_run:
        MANIFEST.write_text(manifest_text)
        print(f"\n✓ Manifest written: {MANIFEST.name}")
    else:
        print("\n(dry-run: manifest not written)")

    # ── Manual review checklist ──────────────────────────────────────────────
    print("""
━━ Manual review needed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Scilab source sha256 (if not passed via --scilab-sha256):
    Download: """ + SCILAB_SOURCE_URL.format(version=new_scilab_version) + """
    Compute:  sha256sum scilab-""" + new_scilab_version + """.tar.bz2

  JavaFX  — check if version changed in ivy.xml or configure.ac
    Pattern: https://repo1.maven.org/maven2/org/openjfx/javafx-{mod}/{ver}/javafx-{mod}-{ver}-linux.jar
    dest-filename must stay as:  javafx.base.jar / javafx.graphics.jar / javafx.swing.jar

  JOGL / Gluegen  — from jogamp.org, not Maven Central
    Check:   https://jogamp.org/deployment/  for new releases
    Minimum version is set in configure.ac: AC_JAVA_CHECK_JAR([jogl],...,2.5)

  JCEF  — from jcefmaven GitHub releases
    Check Scilab source for JCEF commit: grep -r 'jcef-' scilab/thirdparty/
    Releases: https://github.com/jcefmaven/jcefmaven/releases
    The commit hash in the JAR filename must match what Scilab expects.

  jgraphx  — from GitHub raw
    Check if Scilab bumped the version in ivy.xml (listed as <!-- COPY -->).
    URL: https://raw.githubusercontent.com/jgraph/jgraphx/v{ver}/lib/jgraphx.jar

  flexdock  — Scilab's GitLab fork (built from source in this manifest)
    Tags: https://gitlab.com/scilab/forge/flexdock/-/tags
    Update the archive URL + sha256 in the flexdock module if version changed.

  jrosetta  — from Debian source (rarely changes)
    Check: http://deb.debian.org/debian/pool/main/libj/libjrosetta-java/

  BWidget  — from tcl-lang.org (rarely changes)
    Check: https://core.tcl-lang.org/bwidget/wiki?name=Download

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


if __name__ == "__main__":
    main()
