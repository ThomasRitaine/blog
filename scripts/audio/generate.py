#!/usr/bin/env python3
"""Narrated audio for blog posts and projects (stdlib only, Python 3.11+).

Pages opt in with the {{ listen() }} shortcode. For each page that needs it:
clean Markdown -> Gemini writes a narration script -> Cloud TTS (Chirp 3 HD)
-> ffmpeg (loudness + AAC) -> Backblaze B2 -> Cloudflare purge of that URL.

Modes (--pages, or $PAGES in CI):
  changed  default on push: pages whose spoken text changed between --before
           and --after, plus published pages that have no audio yet
  missing  only published pages that have no audio yet
  all      every published page (after changing the voice or the prompt)
  <list>   space-separated slugs or paths, e.g. "posts/my-post"
"""

import argparse, base64, hashlib, io, json, os, re, shutil, subprocess, sys
import tempfile, time, tomllib, urllib.error, urllib.parse, urllib.request, wave
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CFG = tomllib.loads((HERE / "config.toml").read_text())
PROMPT = (HERE / "prompt.md").read_text()
START = time.monotonic()
OPT_IN = re.compile(r"\{\{\s*<\s*listen\b")
INCLUDE_DRAFTS = False


def log(msg):
    print(msg, flush=True)


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"missing environment variable {name}")
    return value


# ---------------------------------------------------------------- HTTP
def http(method, url, *, headers=None, data=None, timeout=120, retries=2):
    hdrs = {"User-Agent": "blog-audio/1", **(headers or {})}
    body = data
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                is_json = r.headers.get_content_type() == "application/json"
                return r.status, json.loads(raw) if raw and is_json else raw
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(
                f"{method} {url.split('?')[0]} -> {e.code}: {text[:400]}"
            ) from None
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(5)
                continue
            raise


# ---------------------------------------------------------------- pages
@dataclass
class Page:
    rel: str  # content/posts/my-post.md
    key: str  # blog/posts/my-post.m4a
    text: str  # cleaned, speakable Markdown (what changes trigger new audio)


FRONT_MATTER = re.compile(r"\A\+\+\+[ \t]*\n(.*?)\n\+\+\+[ \t]*(?:\n|\Z)", re.S)
DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[_-]")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def url_path(rel, fm):
    """Mirror Zola's URL for a page: content/posts/x.md -> posts/x."""
    if fm.get("path"):
        return fm["path"].strip("/")
    *dirs, name = Path(rel).relative_to("content").parts
    stem = Path(name).stem
    if stem == "index":  # colocated page: posts/x/index.md
        stem = dirs.pop()
    stem = fm.get("slug") or DATE_PREFIX.sub("", stem)
    return "/".join(slugify(p) for p in [*dirs, stem])


def page_from(rel, source):
    if (
        not rel
        or source is None
        or not rel.endswith(".md")
        or Path(rel).name.startswith("_index")
    ):
        return None
    m = FRONT_MATTER.match(source)
    if not m:
        return None
    try:
        fm = tomllib.loads(m.group(1))
    except tomllib.TOMLDecodeError as e:
        log(f"! {rel}: bad front matter ({e})")
        return None
    body = source[m.end() :]
    if (fm.get("draft") and not INCLUDE_DRAFTS) or not OPT_IN.search(body):
        return None
    key = f"{CFG['key_prefix']}{url_path(rel, fm)}.m4a"
    return Page(rel, key, clean(fm.get("title", ""), body))


def slug_of(page):
    return page.key.removeprefix(CFG["key_prefix"]).removesuffix(".m4a")


def all_pages():
    for section in CFG["sections"]:
        for f in sorted((ROOT / "content" / section).rglob("*.md")):
            page = page_from(f.relative_to(ROOT).as_posix(), f.read_text())
            if page:
                yield page


# ---------------------------------------------------------------- Markdown cleaning
def attr(text, name):
    m = re.search(rf"\b{name}\s*=\s*[\"']([^\"']*)[\"']", text)
    return m.group(1) if m else ""


def image(alt, caption=""):
    bits = [f'alt="{alt.strip()}"' if alt.strip() else "no alt text"]
    if caption.strip():
        bits.append(f'caption="{caption.strip()}"')
    return f"\n[IMAGE: {', '.join(bits)}]\n"


def _code(m):
    lang = re.split(r"[\s,{]", m.group(2).strip() or "unknown")[0] or "unknown"
    lines = sum(1 for line in m.group(3).splitlines() if line.strip())
    return f"\n[CODE BLOCK: language={lang}, {lines} lines]\n"


def _block(m):
    name, args, inner = m.group(1).split(".")[-1], m.group(2), m.group(3).strip()
    if name == "mermaid":
        return f"\n[DIAGRAM]\n{inner}\n[END DIAGRAM]\n"
    label = attr(args, "title") or attr(args, "type") or name
    return f"\n[ASIDE ({label})]\n{inner}\n[END ASIDE]\n"


def _shortcode(m):
    name, args = m.group(1), m.group(2)
    if "image" in name or re.search(r"\bsrc\s*=", args):
        return image(
            attr(args, "alt") or attr(args, "default_alt"), attr(args, "caption")
        )
    return ""  # listen() and anything else that renders no prose


def _table(m):
    rows = [
        l
        for l in m.group(0).splitlines()
        if l.strip() and not re.fullmatch(r"[\s|:-]+", l)
    ]
    return "\n[TABLE]\n" + "\n".join(rows) + "\n[END TABLE]\n"


def clean(title, body):
    t = body.replace("\r\n", "\n")
    t = re.sub(r"\{%\s*(?:end)?raw\s*%\}", "", t)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"(?ms)^(`{3,}|~{3,})([^\n]*)\n(.*?)^\1[ \t]*$", _code, t)
    inline = []  # protect inline code from the HTML/link rules below
    t = re.sub(
        r"`[^`\n]+`",
        lambda m: inline.append(m.group(0)) or f"\x00{len(inline) - 1}\x00",
        t,
    )
    t = re.sub(
        r"(?s)\{%\s*<\s*([\w.]+)([^%]*?)>\s*%\}(.*?)\{%\s*</\s*\1\s*>\s*%\}", _block, t
    )
    t = re.sub(r"(?s)\{\{\s*<\s*([\w.]+)(.*?)/>\s*\}\}", _shortcode, t)
    t = re.sub(
        r'!\[([^\]]*)\]\(\s*[^)\s]+(?:\s+"([^"]*)")?\s*\)',
        lambda m: image(m.group(1), m.group(2) or ""),
        t,
    )
    t = re.sub(
        r"<img\b[^>]*>",
        lambda m: image(attr(m.group(0), "alt"), attr(m.group(0), "title")),
        t,
    )
    t = re.sub(r"(?m)^\[\^?[^\]]+\]:.*$", "", t)  # link and footnote definitions
    t = re.sub(r"\[\^[^\]]+\]", "", t)  # footnote references
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # [text](url) -> text
    t = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", t)  # [text][ref] -> text
    t = re.sub(r"<https?://[^>]+>|https?://\S+", "[URL]", t)
    t = re.sub(r"</?[A-Za-z][^>]*>", "", t)  # remaining HTML tags
    t = re.sub(r"(?m)^(?:\|.*\n?)+", _table, t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: inline[int(m.group(1))], t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return f"# {title}\n\n{t}\n"


# ---------------------------------------------------------------- Gemini
def check_script(page, script):
    bad = [
        s
        for s in (
            "```",
            "`",
            "http://",
            "https://",
            "{{",
            "{%",
            "[CODE",
            "[IMAGE",
            "[TABLE",
            "](",
        )
        if s in script
    ]
    if bad:
        raise RuntimeError(f"script contains {bad}")
    ratio = len(script.split()) / max(1, len(page.text.split()))
    if not 0.3 <= ratio <= 1.6:
        raise RuntimeError(f"script length ratio {ratio:.2f} is out of bounds")


def write_script(page):
    last = None
    for model in CFG["models"]:
        try:
            _, r = http(
                "POST",
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": env("GEMINI_API_KEY")},
                data={
                    "system_instruction": {"parts": [{"text": PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": page.text}]}],
                },
                timeout=240,
            )
            cand = (r.get("candidates") or [{}])[0]
            if cand.get("finishReason") != "STOP":
                raise RuntimeError(
                    f"finishReason={cand.get('finishReason')} {r.get('promptFeedback', '')}"
                )
            parts = cand.get("content", {}).get("parts", [])
            script = "\n".join(
                p["text"] for p in parts if "text" in p and not p.get("thought")
            ).strip()
            check_script(page, script)
            log(f"  script: {model}, {len(script)} chars")
            return script
        except Exception as e:
            last = e
            log(f"  {model} failed: {e}")
    raise RuntimeError(f"no model produced a usable script ({last})")


# ---------------------------------------------------------------- TTS + ffmpeg
def chunks(script, limit):
    out, cur = [], ""
    for para in (p.strip() for p in script.split("\n\n")):
        if not para:
            continue
        pieces = (
            [para] if len(para.encode()) <= limit else re.split(r"(?<=[.!?])\s+", para)
        )
        for piece in pieces:
            if len(piece.encode()) > limit:
                raise RuntimeError("one sentence is longer than the TTS limit")
            joined = f"{cur}\n\n{piece}" if cur else piece
            if len(joined.encode()) > limit:
                out.append(cur)
                cur = piece
            else:
                cur = joined
    if cur:
        out.append(cur)
    return out


def synthesize(text):
    rate = CFG["sample_rate"]
    audio = {"audioEncoding": "LINEAR16", "sampleRateHertz": rate}
    if CFG.get("speaking_rate", 1.0) != 1.0:
        audio["speakingRate"] = CFG["speaking_rate"]
    voice = CFG["voice"]
    _, r = http(
        "POST",
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        headers={"x-goog-api-key": env("GOOGLE_TTS_API_KEY")},
        data={
            "input": {"text": text},
            "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
            "audioConfig": audio,
        },
    )
    with wave.open(
        io.BytesIO(base64.b64decode(r["audioContent"]))
    ) as w:  # LINEAR16 comes with a WAV header
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, rate):
            raise RuntimeError("unexpected WAV format from TTS")
        return w.readframes(w.getnframes())


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def render(script, workdir):
    rate = CFG["sample_rate"]
    parts = chunks(script, CFG["chunk_bytes"])
    log(f"  tts: {len(parts)} chunk(s), voice {CFG['voice']}")
    silence = b"\x00\x00" * int(rate * CFG["pause_seconds"])
    wav = workdir / "narration.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(silence.join(synthesize(p) for p in parts))

    L = CFG["loudness"]
    base = f"loudnorm=I={L['I']}:TP={L['TP']}:LRA={L['LRA']}"
    err = run(
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(wav),
        "-af",
        f"{base}:print_format=json",
        "-f",
        "null",
        "-",
    ).stderr
    m = json.loads(err[err.rindex("{") : err.rindex("}") + 1])
    second = (
        f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
        f":offset={m['target_offset']}:linear=true"
    )
    out = workdir / "narration.m4a"
    run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav),
        "-af",
        second,
        "-ar",
        str(rate),
        "-ac",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        CFG["bitrate"],
        "-movflags",
        "+faststart",
        str(out),
    )
    duration = run(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(out),
    ).stdout.strip()
    return out, f"{float(duration):.3f}"


# ---------------------------------------------------------------- Backblaze B2 (native API v3)
class B2:
    API = "https://api.backblazeb2.com/b2api/v3"

    def __init__(self):
        basic = base64.b64encode(
            f"{env('B2_KEY_ID')}:{env('B2_APPLICATION_KEY')}".encode()
        ).decode()
        _, r = http(
            "GET",
            f"{self.API}/b2_authorize_account",
            headers={"Authorization": f"Basic {basic}"},
        )
        storage = r["apiInfo"]["storageApi"]
        self.url, self.token = storage["apiUrl"], r["authorizationToken"]
        name = env("B2_BUCKET")
        allowed = (storage.get("allowed") or {}).get("buckets") or []
        ids = [b["id"] for b in allowed if b.get("name") == name]
        if not ids:
            _, lb = self.call(
                "b2_list_buckets", {"accountId": r["accountId"], "bucketName": name}
            )
            ids = [b["bucketId"] for b in lb.get("buckets", [])]
        if not ids:
            raise RuntimeError("the bucket in B2_BUCKET isn't visible to this key")
        self.bucket = ids[0]

    def call(self, op, data):
        return http(
            "POST",
            f"{self.url}/b2api/v3/{op}",
            headers={"Authorization": self.token},
            data=data,
        )

    def names(self, prefix):
        found, start = set(), None
        while True:
            req = {"bucketId": self.bucket, "prefix": prefix, "maxFileCount": 1000}
            if start:
                req["startFileName"] = start
            _, r = self.call("b2_list_file_names", req)
            found |= {f["fileName"] for f in r["files"]}
            start = r.get("nextFileName")
            if not start:
                return found

    def versions(self, name):
        _, r = self.call(
            "b2_list_file_versions",
            {
                "bucketId": self.bucket,
                "prefix": name,
                "startFileName": name,
                "maxFileCount": 100,
            },
        )
        return [f for f in r["files"] if f["fileName"] == name]  # newest first

    def delete(self, name, keep=None):
        for f in self.versions(name):
            if f["fileId"] != keep:
                self.call(
                    "b2_delete_file_version", {"fileId": f["fileId"], "fileName": name}
                )

    def upload(self, name, path, duration):
        data = path.read_bytes()
        for attempt in (1, 2):  # B2 asks for a fresh upload URL after a failure
            _, u = self.call("b2_get_upload_url", {"bucketId": self.bucket})
            try:
                _, r = http(
                    "POST",
                    u["uploadUrl"],
                    data=data,
                    retries=0,
                    timeout=300,
                    headers={
                        "Authorization": u["authorizationToken"],
                        "X-Bz-File-Name": urllib.parse.quote(name, safe="/"),
                        "Content-Type": "audio/mp4",
                        "X-Bz-Content-Sha1": hashlib.sha1(data).hexdigest(),
                        "X-Bz-Info-b2-cache-control": urllib.parse.quote(
                            CFG["cache_control"], safe=""
                        ),
                        "X-Bz-Info-duration": duration,
                    },
                )
                break
            except Exception:
                if attempt == 2:
                    raise
        self.delete(name, keep=r["fileId"])  # drop older versions

    def copy(self, src, dst):
        latest = self.versions(src)
        if not latest or latest[0].get("action") != "upload":
            return False
        _, r = self.call(
            "b2_copy_file", {"sourceFileId": latest[0]["fileId"], "fileName": dst}
        )
        self.delete(dst, keep=r["fileId"])
        return True


# ---------------------------------------------------------------- Cloudflare
def cdn_url(key):
    return f"{CFG['cdn_base']}/{key.removeprefix(CFG['key_prefix'])}"


def purge(urls):
    zone = CFG["cloudflare_zone_id"]
    if zone == "REPLACE_ME":
        raise RuntimeError("set cloudflare_zone_id in scripts/audio/config.toml")
    for i in range(0, len(urls), 30):
        _, r = http(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
            headers={"Authorization": f"Bearer {env('CF_PURGE_TOKEN')}"},
            data={"files": urls[i : i + 30]},
        )
        if not r.get("success"):
            raise RuntimeError(f"purge failed: {r.get('errors')}")
    log(f"purged {len(urls)} URL(s)")
    for u in urls:  # warm the edge; deleted files answer 404, which is fine
        try:
            http("HEAD", u, retries=0, timeout=30)
        except Exception:
            pass


# ---------------------------------------------------------------- git
def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def show(rev, rel):
    p = git("show", f"{rev}:{rel}")
    return p.stdout if p.returncode == 0 else None


def valid_rev(rev):
    return (
        bool(rev)
        and set(rev) != {"0"}
        and git("cat-file", "-e", f"{rev}^{{commit}}").returncode == 0
    )


def changed_pairs(before, after):
    out = git(
        "diff",
        "--name-status",
        "-M",
        "-z",
        before,
        after,
        "--",
        *[f"content/{s}" for s in CFG["sections"]],
    ).stdout.split("\0")
    pairs, i = [], 0
    while i < len(out) and out[i]:
        status = out[i]
        if status[0] == "R":
            pairs.append((out[i + 1], out[i + 2]))
            i += 3
        elif status[0] == "C":
            pairs.append((None, out[i + 2]))
            i += 3
        else:
            pairs.append((out[i + 1], out[i + 1]))
            i += 2
    return pairs


def skip_requested(before, after):
    messages = git("log", "--format=%B", f"{before}..{after}").stdout
    return re.search(r"(?im)^audio:\s*skip\s*$", messages) is not None


# ---------------------------------------------------------------- plan + run
def plan(args, b2):
    mode = args.pages.strip()
    current = {p.key: p for p in all_pages()}
    generate, copies, deletes = {}, [], []

    if mode == "all":
        generate = dict(current)
    elif mode not in ("changed", "missing"):
        wanted = set(mode.split())
        for p in current.values():
            if wanted & {p.rel, slug_of(p), Path(slug_of(p)).name}:
                generate[p.key] = p
        if not generate:
            log(f"! no published page with {{{{ listen() }}}} matches: {mode}")

    if mode == "changed":
        if valid_rev(args.before) and valid_rev(args.after):
            skip = skip_requested(args.before, args.after)
            for old_rel, new_rel in changed_pairs(args.before, args.after):
                old = (
                    page_from(old_rel, show(args.before, old_rel)) if old_rel else None
                )
                new = page_from(new_rel, show(args.after, new_rel))
                if old and (not new or old.key != new.key):
                    deletes.append(old.key)
                if not new or (old and old.key == new.key and old.text == new.text):
                    continue  # page gone, or only non-spoken parts changed
                if old and old.text == new.text:
                    copies.append((old.key, new))  # moved, same words
                elif old and skip:
                    log(f"- {new.rel}: changed, but a commit says 'Audio: skip'")
                else:
                    generate[new.key] = new
        else:
            log(
                f"! can't diff {args.before}..{args.after}; only filling in missing audio"
            )

    if mode in ("changed", "missing") and b2:
        have = b2.names(CFG["key_prefix"])
        moved_to = {p.key for _, p in copies}
        for key, p in current.items():
            if key not in have and key not in moved_to:
                generate.setdefault(key, p)

    keep = set(generate) | {p.key for _, p in copies}
    return generate, copies, [k for k in dict.fromkeys(deletes) if k not in keep]


def main():
    global INCLUDE_DRAFTS
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pages", default=os.environ.get("PAGES") or "changed")
    ap.add_argument("--before", default=os.environ.get("BEFORE") or "HEAD~1")
    ap.add_argument("--after", default=os.environ.get("AFTER") or "HEAD")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="write to .audio-out/ instead of uploading",
    )
    ap.add_argument(
        "--plan",
        action="store_true",
        help="print what would happen, write cleaned.md, no API calls",
    )
    ap.add_argument(
        "--drafts",
        action="store_true",
        help="include drafts (only with --dry-run or --plan)",
    )
    args = ap.parse_args()
    if args.drafts and not (args.dry_run or args.plan):
        ap.error("--drafts needs --dry-run or --plan")
    INCLUDE_DRAFTS = args.drafts

    b2 = None if (args.dry_run or args.plan) else B2()
    generate, copies, deletes = plan(args, b2)

    if args.plan:
        for p in generate.values():
            out = ROOT / ".audio-out" / slug_of(p)
            out.mkdir(parents=True, exist_ok=True)
            (out / "cleaned.md").write_text(p.text)
            log(f"generate  {p.rel} -> {p.key}")
        for src, p in copies:
            log(f"copy      {src} -> {p.key}")
        for k in deletes:
            log(f"delete    {k}")
        if not (generate or copies or deletes):
            log("nothing to do")
        return

    touched, failures, leftover = [], [], []

    for src, page in copies:
        log(f"> {page.rel}: moved, copying {src} -> {page.key}")
        if not args.dry_run:
            if b2.copy(src, page.key):
                touched.append(page.key)
            else:
                generate.setdefault(page.key, page)

    for page in generate.values():
        if time.monotonic() - START > CFG["time_budget_seconds"]:
            leftover.append(slug_of(page))
            continue
        log(f"> {page.rel} -> {page.key}")
        try:
            out = ROOT / ".audio-out" / slug_of(page)
            if args.dry_run:
                out.mkdir(parents=True, exist_ok=True)
                (out / "cleaned.md").write_text(page.text)
            with tempfile.TemporaryDirectory() as tmp:
                script = write_script(page)
                if args.dry_run:
                    (out / "script.txt").write_text(script)
                m4a, duration = render(script, Path(tmp))
                if args.dry_run:
                    shutil.copy(m4a, out / "audio.m4a")
                    log(f"  wrote {out.relative_to(ROOT)}/ ({duration} s)")
                else:
                    b2.upload(page.key, m4a, duration)
                    touched.append(page.key)
                    log(f"  uploaded ({duration} s)")
        except Exception as e:
            failures.append(slug_of(page))
            log(f"! {page.rel}: {e}")

    for key in deletes:
        log(f"> deleting {key}")
        if not args.dry_run:
            b2.delete(key)
            touched.append(key)

    if touched:
        purge([cdn_url(k) for k in touched])
    if not (generate or copies or deletes):
        log("nothing to do")
    if leftover:
        log(
            "! out of time; run the audio workflow by hand with pages: "
            + " ".join(leftover)
        )
    if failures:
        log("! failed: " + " ".join(failures))
    sys.exit(1 if failures or leftover else 0)


if __name__ == "__main__":
    main()
