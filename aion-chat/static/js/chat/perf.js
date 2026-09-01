(function (global) {
  const hasPerf = !!(global.performance && typeof global.performance.now === "function");
  const start = hasPerf ? global.performance.now() : Date.now();
  const marks = [];
  const seen = new Set();

  function now() {
    return hasPerf ? global.performance.now() : Date.now();
  }

  function mark(name, detail) {
    const entry = {
      name: String(name || ""),
      at_ms: Math.round((now() - start) * 10) / 10,
      detail: detail || null,
    };
    marks.push(entry);
    if (hasPerf && entry.name) {
      try { global.performance.mark("chat:" + entry.name); } catch (e) {}
    }
    return entry;
  }

  function markOnce(name, detail) {
    if (seen.has(name)) return null;
    seen.add(name);
    return mark(name, detail);
  }

  global.ChatPerf = {
    mark,
    markOnce,
    getMarks() { return marks.slice(); },
  };

  mark("script_boot");
  document.addEventListener("DOMContentLoaded", () => markOnce("dom_content_loaded"));
  global.addEventListener("load", () => markOnce("window_load"));
})(window);
