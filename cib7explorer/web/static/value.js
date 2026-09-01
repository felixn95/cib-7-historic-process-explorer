/* The value dialog: fetch the full value on click, format it, copy it.
 *
 * Deliberately without a framework and without dependencies. Uses the native <dialog> element,
 * and keeps the table compact -- a few thousand characters of JSON inside a table cell help
 * nobody.
 */
(function () {
  "use strict";

  let raw = "";          // the value as stored in the database
  let formatted = "";    // indented, if JSON or XML
  let mode = "formatted";

  function dialog() { return document.getElementById("value-dialog"); }
  function field() { return document.getElementById("value-content"); }

  function indentJson(text) {
    try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return null; }
  }

  function indentXml(text) {
    let depth = 0;
    return text.replace(/>\s*</g, ">\n<").split("\n").map(function (row) {
      if (/^<\//.test(row)) depth = Math.max(0, depth - 1);
      const out = "  ".repeat(depth) + row;
      if (/^<[^/!?][^>]*[^/]>$/.test(row)) depth += 1;
      return out;
    }).join("\n");
  }

  function render() {
    const f = field();
    if (!f) return;
    f.textContent = (mode === "raw" || !formatted) ? raw : formatted;
    const button = document.getElementById("value-mode");
    if (button) {
      button.textContent = mode === "raw" ? "show formatted" : "show raw";
      button.hidden = !formatted;
    }
  }

  window.openValue = function (url, name, type) {
    const d = dialog();
    if (!d) return;
    document.getElementById("value-name").textContent = name;
    document.getElementById("value-type").textContent = type || "";
    document.getElementById("value-meta").textContent = "loading …";
    field().textContent = "";
    raw = ""; formatted = ""; mode = "formatted";
    if (typeof d.showModal === "function") { d.showModal(); } else { d.setAttribute("open", ""); }

    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        raw = data.value || data.raw_bytes || "";
        if (!raw) {
          document.getElementById("value-meta").textContent =
            data.reason || data.hint || "No readable value.";
          return;
        }
        if (data.formattable) {
          formatted = (type === "xml") ? indentXml(raw) : indentJson(raw);
        }
        const parts = [];
        if (data.size !== null && data.size !== undefined) parts.push(data.size + " bytes");
        if (data.java_class) parts.push(data.java_class);
        if (data.occurrences > 1) parts.push(data.occurrences + " occurrences in this instance (most recent shown)");
        if (data.hint) parts.push(data.hint);
        document.getElementById("value-meta").textContent = parts.join(" · ");
        render();
      })
      .catch(function (e) {
        document.getElementById("value-meta").textContent = "Loading failed: " + e;
      });
  };

  /* Copy to the clipboard. Two routes, because the table carries only a preview: when the value
     is fully present (data-text) it is copied directly; otherwise it is fetched via data-url and
     then copied -- what gets copied is always the FULL value, never the truncated preview. */
  function toClipboard(text, button) {
    function feedback(ok) {
      button.classList.add(ok ? "copied" : "copy-failed");
      const previousTitle = button.getAttribute("title");
      button.setAttribute("title", ok ? "copied" : "Copying not possible — the text is selected");
      setTimeout(function () {
        button.classList.remove("copied", "copy-failed");
        button.setAttribute("title", previousTitle);
      }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { feedback(true); },
                                              function () { fallbackCopy(text, button); });
    } else {
      fallbackCopy(text, button);
    }
  }

  /* Older browsers and non-HTTPS contexts: hidden field, execCommand, otherwise select. */
  function fallbackCopy(text, button) {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.left = "-9999px";
    document.body.appendChild(helper);
    helper.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(helper);
    button.classList.add(ok ? "copied" : "copy-failed");
    setTimeout(function () { button.classList.remove("copied", "copy-failed"); }, 1400);
  }

  document.addEventListener("click", function (ev) {
    const copier = ev.target.closest ? ev.target.closest(".copy-button") : null;
    if (copier) {
      ev.preventDefault();
      ev.stopPropagation();
      const direct = copier.getAttribute("data-text");
      if (direct !== null) {
        toClipboard(direct, copier);
      } else {
        const url = copier.getAttribute("data-url");
        fetch(url, { headers: { "Accept": "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            const text = data.value || data.raw_bytes || "";
            if (text) { toClipboard(text, copier); }
            else { copier.classList.add("copy-failed");
                   setTimeout(function () { copier.classList.remove("copy-failed"); }, 1400); }
          })
          .catch(function () {
            copier.classList.add("copy-failed");
            setTimeout(function () { copier.classList.remove("copy-failed"); }, 1400);
          });
      }
      return;
    }
    const target = ev.target;
    if (target.id === "value-mode") {
      mode = (mode === "raw") ? "formatted" : "raw";
      render();
    } else if (target.id === "value-copy") {
      toClipboard(field().textContent, target);
      target.textContent = "copied";
      setTimeout(function () { target.textContent = "copy"; }, 1400);
    } else if (target.id === "value-close") {
      const d = dialog();
      if (d && typeof d.close === "function") { d.close(); } else if (d) { d.removeAttribute("open"); }
    }
  });
})();
