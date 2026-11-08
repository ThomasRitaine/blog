// Sends Umami "scroll-depth" events at 25/50/75/90% of the page.
(() => {
  const marks = [25, 50, 75, 90];
  const sent = new Set();

  const onScroll = () => {
    if (typeof window.umami === "undefined") return; // tracker not loaded yet (or blocked)
    const doc = document.documentElement;
    if (doc.scrollHeight <= window.innerHeight) return; // page fits on screen, nothing to measure

    const seen = Math.round(((window.scrollY + window.innerHeight) / doc.scrollHeight) * 100);
    for (const m of marks) {
      if (seen >= m && !sent.has(m)) {
        sent.add(m);
        window.umami.track("scroll-depth", { depth: m });
      }
    }
    if (sent.size === marks.length) window.removeEventListener("scroll", onScroll);
  };

  window.addEventListener("scroll", onScroll, { passive: true });
})();
