+++
title = "Building an AI-Narrated Audio Pipeline for My Blog"
date = 2026-11-13
description = "How I built a free, lightweight pipeline to generate AI-narrated audio for my blog posts."

[taxonomies]
tags = ["python", "audio", "tts", "cloudflare", "cdn", "self-hosting"]

[extra]
toc = true
mermaid = true
stylesheets = ["css/mermaid.css"]
+++

{{ <listen /> }}

At my work at Google, we love "one-pagers", those design documents for engineering decisions that are almost always way more than one page long. So today I am writing that post to explain that personal project, which is technically one webpage long.

That feature is live already, you have seen the play button above, and you might even be listening to it now.

Here is the full story of how I designed and built an AI-narrated audio pipeline for my posts, the few iterations it took to make it simple, and the bugs I solved. At a high level, a CI job cleans the Markdown, asks Gemini to write a spoken script, generates the voice with Google Cloud Text-to-Speech, and encodes a small `.m4a` file with `ffmpeg`. That file is uploaded to S3-like object storage on Backblaze B2, served behind my Cloudflare CDN, and played by a tiny custom JavaScript player.

## Why Not Just Plain Text-to-Speech ?

Have you ever listened to a technical blog post using a basic text-to-speech reader ? It is weird. It happily reads out `bash` flags letter by letter, spells out raw URLs "H-T-T-P-S colon slash slash", reads the Markdown tables row by row, and announces footnote numbers in the middle of a sentence.

I wanted something that actually feels like a human reading an adapted version of the post aloud. Before writing any code, I set a few simple ground rules:

- **Adapt first, speak second:** Use an LLM to turn the post into a natural spoken script before generating audio. Describe code blocks in one plain sentence instead of reading code aloud, announce images using their alt text and caption, summarize tables, and skip URLs and footnotes.
- **Keep it free (or close to zero):** Stay inside generous free tiers without sacrificing voice quality.
- **Match this blog's lightweight spirit:** No npm build step, no frontend framework, and zero third-party Python libraries. Everything runs on the Python standard library, `ffmpeg`, about 4 KB of vanilla JavaScript, and 2 KB of CSS.
- **Never block a deploy:** Site builds and audio generation must stay completely decoupled. If an API is having a bad day, the blog still deploys as usual.
- **Only rebuild when spoken words change:** Fixing a typo in a tag should not rebuild the entire audio for that post.

## How the Pipeline Works

The main design property is that **the static site and the audio files are completely independent**. The audio files live on Backblaze B2 object storage, plugged behind my Cloudflare CDN at `cdn.thomas.ritaine.com`. When the CI builds the blog, it does not know whether an audio file for a given post exists or not. The browser checks the CDN at runtime, when the page loads, with a request under 1 KB resolving in average in 50 milliseconds total with `DNS`+`TLS`. It checks if there is an audio file for that post, and how long it is.

{% <mermaid invertible={false} full_width={false}> %}
flowchart TB
  PUSH(["git push (main)"])
  subgraph SITE["Site Deploy (build.yml)"]
    ZOLA["Static Site Build"]:::muted --> BLOG["thomas.ritaine.com"]:::muted
  end
  subgraph CI["Audio Pipeline (audio.yml)"]
    CLEAN["Python Regex Cleaner"] --> GEMINI["Gemini 3.8 Flash"]
    GEMINI -- "spoken script" --> TTS["Cloud TTS\n(Chirp 3 HD)"]
    TTS -- "lossless WAV" --> FFMPEG["ffmpeg\n(.m4a encode)"]
  end
  B2[("Backblaze B2")]
  CDN["Cloudflare CDN"]:::accent
  BR(["Visitor Browser (you right now)\n(listen.js)"]):::accent
  PUSH --> ZOLA
  PUSH --> CLEAN
  FFMPEG -- "Upload + duration header" --> B2
  B2 -- "Origin" --> CDN
  FFMPEG -. "Purge URL" .-> CDN
  BLOG -- "HTML + 6 KB JS" --> BR
  BR -- "1. HEAD (duration)" --> CDN
  CDN -- "2. 206 Range (.m4a)" --> BR
{% </mermaid> %}

Here is how it works step by step:

1. **Opt-in per page:** I place a custom `<listen />` component at the top of a Markdown post. Adding that single tag renders the player HTML and tells the CI script that this page should have audio.
2. **Separate CI workflow:** Pushing to `main` on Codeberg runs two jobs in parallel on Forgejo Actions: `build.yml` builds the static site container image, and `audio.yml` runs the audio pipeline. The audio job compares the `before` and `after` commits to see if there is a new article to generate audio for, or if we need to regenerate the audio for an existing article.
3. **Deterministic Markdown cleaning:** Before calling any AI model, `scripts/audio/generate.py` uses regular expressions to remove HTML comments, footnotes, and bare URLs, and replaces non-spoken blocks with clear markers such as `[CODE BLOCK: language=bash, 2 lines]`, `[IMAGE: alt="...", caption="..."]`, `[TABLE]...[END TABLE]`, and `[ASIDE]...[END ASIDE]`. Cleaning the text first uses fewer tokens, makes the output more predictable, and gives the LLM much less room to invent details about the code.
4. **Script generation with Gemini:** The cleaned text is sent to `gemini-3.8-flash` with a system prompt. The script checks the response before accepting it: `finishReason` must be `STOP`, the word count must stay between `0.3x` and `1.6x` of the cleaned text, and the text cannot contain backticks, links, or leftover markers. If that validation fails or the API returns an error, the script retries with `gemini-3.5-flash-lite`.
5. **Lossless speech synthesis:** Google Cloud Text-to-Speech using `Chirp 3 HD` has a hard limit of 5,000 bytes per request. My script splits the narration into chunks of at most 3,000 bytes at paragraph boundaries. When a paragraph is too long, it splits at sentence boundaries instead so it never cuts mid-sentence. It then requests uncompressed 24 kHz mono `LINEAR16` WAV audio, and joins the chunks with `0.6 s` pauses. For the voice, I am using `en-US-Chirp3-HD-Fenrir` because my wife likes it, you expected an engineering decision on that ?
6. **One single lossy encode:** `ffmpeg` runs a two-pass loudness normalization to `-16 LUFS` and `-1.5 dBTP` true peak, with `linear=true` so the voice is not compressed, and then encodes the audio once into a mono 48 kbps AAC `.m4a` file with `-movflags +faststart`. Normalizing loudness makes every post play at the same podcast volume and evens out small level differences between TTS chunks. Encoding only once from lossless WAV avoids losing audio quality, mono cuts the file size in half since stereo adds nothing to speech, and `+faststart` moves the MP4 index to the beginning of the file so playback and seeking start right away.
7. **Backblaze B2 upload and Cloudflare purge:** The script uploads the `.m4a` file to my Backblaze B2 bucket via its native v3 API, stores the exact duration in a custom `X-Bz-Info-duration` header, and deletes older file versions in B2. Because `cdn.thomas.ritaine.com` sits in front of that B2 bucket, the script then purges that exact URL on Cloudflare and sends one `HEAD` request to warm the nearest edge cache.

On the visitor's browser, `listen.js` sends a small `HEAD` request to `https://cdn.thomas.ritaine.com/blog/posts/<slug>.m4a`. If it gets a `404`, the placeholder disappears. If it gets a `200`, it reads the duration header, fades in the player, and sets `preload="metadata"`, which downloads around 100 to 150 KB for the MP4 index. As soon as your mouse pointer, keyboard focus, or finger gets close to the player, it switches to `preload="auto"` to start buffering the audio. We do that to have the smoothest user experience possible: there is no waiting time when pressing play, and no useless downloads when the page loads if you only came to read.

## Main Engineering Decisions

It took five design iterations to make the system simple. Along the way, I explored section-level hashes, cached FLAC chunks, S3 request signing, `?v=hash` cache busting, and a `manifest.json` file, before removing all of them. Here are the main decisions behind the final setup.

### 1. Choosing the Text-to-Speech Provider

My first idea was Cloudflare Workers AI, since my CDN already uses Cloudflare and it offers 10,000 free neurons per day. Then I looked at the numbers:

| Option | Free Allowance | Limitation |
| :--- | :--- | :--- |
| Cloudflare Aura-2 (`aura-2-en`) | ~3,600 chars/day (~4 min audio) | A 1,500-word post takes 2–3 days |
| Cloudflare MeloTTS | — | Shut down on 20 Nov 2026 (`HTTP 410`) |
| Gemini Preview TTS (`2.5-flash-preview-tts`) | Unpublished | Retiring on 17 Nov 2026 |
| ElevenLabs Free | 10,000 chars/month | Too small, requires attribution, non-commercial |
| Self-hosted `Kokoro-82M` | Unlimited (Apache 2.0) | ~2x real-time on CPU, too slow for a 5-min CI job |
| **Google Cloud TTS (`Chirp 3 HD`)** | **1,000,000 chars/month** | **Needs billing enabled, mitigated with a budget alert** |

`Chirp 3 HD` was the clear choice: one million free characters per month covers around 100 full regenerations of a 10,000-character post, so an entire post finishes in a single CI run without needing logic to pause and resume the next day.

### 2. Why Two Separate Google Cloud Projects ?

I ran into an unexpected Google Cloud rule: **enabling billing on a project removes its free Gemini API allowance**, so every prompt starts being billed. At the same time, Cloud Text-to-Speech requires billing to be enabled, even when staying inside the monthly free tier.

The solution was to use two separate projects:

- **`blog-llm` (Billing OFF):** Runs the Gemini API on the free tier with an AI Studio authorization key.
- **`blog-tts` (Billing ON):** Runs Cloud Text-to-Speech with a budget alert and an API key restricted to that single API.

Using plain `x-goog-api-key` headers for both services also meant I did not need OAuth service-account JWT signing, which the Python standard library cannot generate on its own without external cryptography packages.

### 3. Audio Format and Encoding Only Once

Every lossy re-encode degrades audio quality. To keep the files small without losing quality, the audio stays lossless as `LINEAR16` PCM WAV from the TTS response through chunk assembly and two-pass loudness normalization, and is encoded to a lossy format **only once** at the very end.

For the output format, I compared Opus, MP3, and AAC:

- **Opus (`.opus` at 32–40 kbps):** Smallest files at ~2.6 MB for 10 minutes, but iOS Safari only added Ogg Opus support in version 18.4, and Opus in MP4 does not play in Safari's `<audio>` element. Serving Opus with an M4A fallback meant managing twice as many files for about 20% smaller size.
- **MP3 (64 kbps mono):** Works everywhere, around 4.8 MB for 10 minutes.
- **AAC-LC in `.m4a` (48 kbps mono + `faststart`):** Works in every browser and every iOS version, at ~0.35 MB per minute, or ~3.3 MB for 10 minutes. My 55-second test post came out at **342 KB**.

### 4. Dropping the Manifest, `?v=hash`, and Partial Rebuilds

In earlier versions of the design, I wanted to hash each section of a post to regenerate only edited paragraphs, keep a `manifest.json` file in Backblaze B2 for the site build to read, and append `?v=<hash>` to audio URLs. Removing all three made everything simpler:

- **Why full-page regeneration is better than splicing:** Generative voices do not read text with the exact same pace and tone across separate runs. Splicing a new paragraph between two older audio chunks creates a noticeable change in voice at the join. With 1M free characters per month, regenerating the whole page produces one coherent narration and removes all cache folders.
- **Why `?v=hash` did not help:** Backblaze B2 ignores unknown query parameters and always serves the latest file at that path anyway. Adding `?v=hash` only complicated Cloudflare caching rules and forced the site build to know the hash in advance.
- **Why no manifest is needed:** Instead of keeping a central JSON file in sync, the browser sends a `HEAD` request to a predictable path like `/blog/posts/<slug>.m4a`, reads `x-bz-info-duration`, and streams audio with HTTP `206` Range requests. Backblaze B2 also has free egress to Cloudflare through the Bandwidth Alliance. When CI uploads a new file, it purges that exact URL on Cloudflare. Cloudflare caches the file for 1 year (`s-maxage=31536000`), and browsers cache it for 1 day (`max-age=86400`).

### 5. Detecting Changes with Git and Uploading to B2

To avoid rebuilding audio when nothing spoken changed, `generate.py` runs `git diff --name-status -M` between the push's `before` and `after` commits and compares the **cleaned** text of both versions:

- Changing tags, dates, or descriptions leaves the cleaned text unchanged, so CI skips the post.
- Renaming a file without changing its text copies the audio server-side via `b2_copy_file` instead of calling TTS again.
- Deleting a post, removing `<listen />`, or setting `draft = true` deletes the audio from B2 and purges Cloudflare.
- Adding `Audio: skip` in a commit message skips regenerating edited posts when I only fix a small typo.
- Every run ends with a "missing" check against B2's `blog/` prefix to catch any post that still has no audio. Codeberg's `codeberg-small` runner has a 5-minute limit, so my script stops starting new pages after 150 seconds, and any remaining pages automatically catch up on the next run.

For storage calls, I chose **Backblaze B2 native API v3** instead of the S3-compatible API. S3 `PutObject` requires AWS SigV4 request signing, which takes a lot of code without `boto3`, while B2's native API uses Basic auth and simple HTTP headers.

### 6. Writing a 8 KB Custom Player

Default `<audio controls>` has no `-10s` / `+10s` buttons and looks different in every browser, while ready-made libraries bring much more code than needed. Writing `listen.js` directly took about 6 KB of JavaScript and 2 KB of CSS, and covers:

- Play/pause, `-10s` / `+10s` skip buttons, and playback speed from `0.75x` up to `2x` saved in `localStorage`.
- A clickable and draggable ARIA slider seek bar with keyboard navigation: `±5s` with arrow keys, `Home`, and `End`.
- Saving and restoring playback position per post every 5 seconds, once you are past the first 5 seconds and more than 10 seconds from the end.
- Lock-screen and headphone buttons through the Media Session API, cookieless Umami events for `audio-play` and `audio-complete`, light and dark theme CSS variables, a `<noscript>` stylesheet that hides the placeholder when JavaScript is off, and `prefers-reduced-motion` support.

## Bugs I Solved

Testing the pipeline end to end uncovered a few interesting problems:

1. **Gemini 3.8 Flash `503` on the first live upload:** On my very first real upload run, `gemini-3.8-flash` returned `503 UNAVAILABLE` due to high demand. The fallback logic immediately switched to `gemini-3.5-flash-lite` and finished the generation. It was reassuring to see the fallback work in practice on day one.
2. **Header visible in `curl`, but unreadable in JavaScript:** Even with CORS configured on `cdn.thomas.ritaine.com`, `fetch()` in the browser returned `null` for `x-bz-info-duration`. Browsers hide custom headers from frontend JavaScript unless the response includes `Access-Control-Expose-Headers: x-bz-info-duration`. I also had to keep an exact list of removed B2 headers like `x-bz-file-id` and `x-bz-content-sha1` in Cloudflare rather than a wildcard rule starting with `x-bz-`, which would have removed my duration header too !

## Verifying Everything from the Terminal

Before testing in the browser, I checked each step of the pipeline from my terminal, from local dry runs to CDN cache headers and byte-range requests.

First, running the cleaner, script generator, TTS synthesis, and `ffmpeg` encode locally without uploading to Backblaze B2:

```bash
python3 scripts/audio/generate.py --dry-run --drafts \
  --pages posts/audio-pipeline-test
ffprobe -v error -show_entries format=duration,size,bit_rate \
  -of default=noprint_wrappers=1 \
  .audio-out/posts/audio-pipeline-test/audio.m4a
```

Once uploaded to Backblaze B2 and warmed on Cloudflare, a `curl` command piped to `grep` checks that caching, CORS header exposure, custom duration metadata, and byte-range support are all present:

```bash
curl -sI https://cdn.thomas.ritaine.com/blog/posts/audio-pipeline-test.m4a \
  | grep -i -e '^http/' -e '^content-type:' -e '^cf-cache-status:' \
    -e '^x-bz-info-duration:' -e '^cache-control:' \
    -e '^access-control-expose-headers:' -e '^accept-ranges:'
```

```http
HTTP/2 200
content-type: audio/mp4
cf-cache-status: HIT
x-bz-info-duration: 61.700
cache-control: public, max-age=86400, s-maxage=31536000
access-control-expose-headers: x-bz-info-duration
accept-ranges: bytes
```

And one final check with a `Range` header to verify that seeking and progressive playback receive `206 Partial Content` instead of downloading the whole file at once:

```bash
curl -sI -H "Range: bytes=0-1023" \
  https://cdn.thomas.ritaine.com/blog/posts/audio-pipeline-test.m4a \
  | grep -iE '^(http/|content-range|content-length|cf-cache-status)'
```

```http
HTTP/2 206
content-length: 1024
cf-cache-status: HIT
content-range: bytes 0-1023/391143
```

Running that command returns `HTTP/2 206` with `content-range: bytes 0-1023/...` and `cf-cache-status: HIT`, confirming that the browser can start playing and jump anywhere in the timeline right away.

## Wrapping Up

In the end, the whole feature comes down to one Python script using only the standard library, `ffmpeg`, two free Google Cloud tiers, Backblaze B2 behind Cloudflare, and 6 KB of JavaScript. The site build does not depend on it, editing tags or dates does not trigger unnecessary audio rebuilds, and if you pressed play at the top of this page, you just heard the pipeline narrate its own design.

:wq
