// Narrated-audio player, loaded by the `listen` shortcode.
(() => {
  "use strict";
  const root = document.querySelector(".listen");
  if (!root) return;
  if (location.hostname.endsWith(".onion")) return root.remove(); // no audio over Tor

  const path = location.pathname.replace(/^\/+|\/+$/g, "");
  const src = `${root.dataset.base}/${path}.m4a`;
  const RATES = [1, 1.25, 1.5, 1.75, 2, 0.75];
  const POS_KEY = `listen.pos:${path}`;
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
    del: (k) => { try { localStorage.removeItem(k); } catch {} },
  };
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  };
  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const ICON = {
    play: "M8 5v14l11-7z",
    pause: "M6 5h4v14H6zm8 0h4v14h-4z",
    skip: "M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z",
  };

  function build(estimate) {
    const a = new Audio();
    a.preload = "metadata";
    a.src = src;
    let total = estimate, pending = null, tracked = false, lastSave = 0;

    root.innerHTML = `
      <button type="button" class="listen-play" aria-label="Play narration">${svg(ICON.play)}</button>
      <button type="button" class="listen-skip" data-skip="-10" aria-label="Back 10 seconds">${svg(ICON.skip)}<span>10</span></button>
      <button type="button" class="listen-skip listen-fwd" data-skip="10" aria-label="Forward 10 seconds">${svg(ICON.skip)}<span>10</span></button>
      <div class="listen-bar" role="slider" tabindex="0" aria-label="Seek" aria-valuemin="0"><div class="listen-fill"></div></div>
      <span class="listen-time"></span>
      <button type="button" class="listen-rate" aria-label="Playback speed"></button>`;
    const $ = (s) => root.querySelector(s);
    const play = $(".listen-play"), bar = $(".listen-bar"), fill = $(".listen-fill");
    const time = $(".listen-time"), rateBtn = $(".listen-rate");

    const draw = (t = a.currentTime) => {
      fill.style.transform = `scaleX(${total ? Math.min(1, t / total) : 0})`;
      bar.setAttribute("aria-valuemax", Math.round(total));
      bar.setAttribute("aria-valuenow", Math.round(t));
      bar.setAttribute("aria-valuetext", fmt(t));
      time.textContent = `${fmt(t)} / ${fmt(total)}`;
    };
    const seek = (t) => {
      t = Math.min(Math.max(0, t), total || 0);
      if (a.readyState >= 1) a.currentTime = t;
      else { pending = t; a.preload = "auto"; a.load(); }
      draw(t);
    };
    const setRate = (r) => {
      a.defaultPlaybackRate = a.playbackRate = r;
      rateBtn.textContent = `${r}×`;
      store.set("listen.rate", r);
    };
    const setIcon = (playing) => {
      play.innerHTML = svg(playing ? ICON.pause : ICON.play);
      play.setAttribute("aria-label", playing ? "Pause narration" : "Play narration");
      root.classList.toggle("is-playing", playing);
    };

    setRate(Number(store.get("listen.rate")) || 1);
    draw(0);

    // Start buffering as soon as the reader shows interest.
    for (const ev of ["pointerenter", "focusin", "touchstart"])
      root.addEventListener(ev, () => { a.preload = "auto"; }, { once: true, passive: true });

    play.addEventListener("click", () => (a.paused ? a.play().catch(() => {}) : a.pause()));
    root.querySelectorAll(".listen-skip").forEach((b) =>
      b.addEventListener("click", () => seek(a.currentTime + Number(b.dataset.skip))));
    rateBtn.addEventListener("click", () => setRate(RATES[(RATES.indexOf(a.playbackRate) + 1) % RATES.length]));

    let dragging = false;
    const fromPointer = (e) => {
      const r = bar.getBoundingClientRect();
      seek(((e.clientX - r.left) / r.width) * total);
    };
    bar.addEventListener("pointerdown", (e) => { dragging = true; bar.setPointerCapture(e.pointerId); fromPointer(e); });
    bar.addEventListener("pointermove", (e) => dragging && fromPointer(e));
    bar.addEventListener("pointerup", () => { dragging = false; });
    bar.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -5, ArrowRight: 5, ArrowDown: -5, ArrowUp: 5 }[e.key];
      if (step) seek(a.currentTime + step);
      else if (e.key === "Home") seek(0);
      else if (e.key === "End") seek(total);
      else return;
      e.preventDefault();
    });

    a.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(a.duration)) total = a.duration;
      const saved = Number(store.get(POS_KEY));
      if (pending !== null) { a.currentTime = pending; pending = null; }
      else if (saved > 5 && saved < total - 10 && a.currentTime === 0) a.currentTime = saved;
      draw();
    });
    a.addEventListener("timeupdate", () => {
      if (!dragging) draw();
      if (Date.now() - lastSave > 5000) { lastSave = Date.now(); store.set(POS_KEY, a.currentTime); }
    });
    a.addEventListener("play", () => {
      setIcon(true);
      if (!tracked && window.umami) { tracked = true; window.umami.track("audio-play"); }
    });
    a.addEventListener("pause", () => setIcon(false));
    a.addEventListener("ended", () => {
      setIcon(false);
      store.del(POS_KEY);
      if (window.umami) window.umami.track("audio-complete");
    });
    a.addEventListener("error", () => { time.textContent = "Audio unavailable"; });

    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: document.title, artist: location.hostname });
      const ms = navigator.mediaSession;
      const on = (action, fn) => { try { ms.setActionHandler(action, fn); } catch {} };
      on("play", () => a.play());
      on("pause", () => a.pause());
      on("seekbackward", (d) => seek(a.currentTime - (d.seekOffset || 10)));
      on("seekforward", (d) => seek(a.currentTime + (d.seekOffset || 10)));
      on("seekto", (d) => seek(d.seekTime));
    }
  }

  fetch(src, { method: "HEAD" })
    .then((r) => {
      if (!r.ok) throw new Error(String(r.status));
      const d = parseFloat(r.headers.get("x-bz-info-duration"));
      const bytes = Number(r.headers.get("content-length")) || 0;
      build(d > 0 ? d : (bytes * 8) / 48000 / 1.055); // fallback: estimate from the file size
      root.classList.add("is-ready");
    })
    .catch(() => root.remove());
})();
