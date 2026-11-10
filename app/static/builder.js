/* The free résumé and CV builder. The draft lives in this browser (localStorage); the server only turns it into a preview or a file. */
(function () {
  "use strict";
  var root = document.getElementById("rb");
  if (!root) return;

  var MODE = root.getAttribute("data-mode") === "cv" ? "cv" : "resume";
  var CV = MODE === "cv";
  var KEY = "cs_builder_v1_" + MODE;
  var LAYOUT_KEY = "cs_builder_layout";
  var SAMPLE = JSON.parse(document.getElementById("rb-sample").textContent);
  var AI = root.getAttribute("data-ai") === "1";
  var formEl = document.getElementById("rb-form");
  var previewEl = document.getElementById("rb-preview");
  var statusEl = document.getElementById("rb-status");
  var layout = "classic";
  var paper = "a4";
  var previewSeq = 0;
  var timer = null;

  // ------------------------------------------------------------------ storage (it can be blocked: the builder still works without it)
  function store(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* ignore */ } }
  function fetchStored(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  function job() { return { title: "", company: "", location: "", start: "", end: "", bullets: "" }; }
  function school() { return { degree: "", school: "", start: "", end: "", details: "" }; }
  function blank() {
    return {
      name: "", contact: { email: "", phone: "", location: "", links: "" }, summary: "",
      experience: [job()], education: [school()], skills: [{ label: "", items: "" }], projects: [], extras: [],
      references: CV ? "Available on request." : ""
    };
  }
  function load() {
    var raw = fetchStored(KEY);
    if (!raw) return null;
    try {
      var s = JSON.parse(raw);
      return s && typeof s === "object" ? Object.assign(blank(), s) : null;
    } catch (e) { return null; }
  }
  function fromSample() {
    var s = JSON.parse(JSON.stringify(SAMPLE));
    s.contact.links = (s.contact.links || []).join("\n");
    ["experience", "projects"].forEach(function (k) { (s[k] || []).forEach(function (x) { x.bullets = (x.bullets || []).join("\n"); }); });
    (s.education || []).forEach(function (x) { x.details = (x.details || []).join("\n"); });
    (s.extras || []).forEach(function (x) { x.items = (x.items || []).join("\n"); });
    return Object.assign(blank(), s);
  }
  var state = load() || blank();
  var savedLayout = fetchStored(LAYOUT_KEY);
  if (savedLayout === "modern" || savedLayout === "compact" || savedLayout === "classic") layout = savedLayout;

  // ------------------------------------------------------------------ small DOM helpers
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  var uid = 0;
  function field(label, obj, key, o) {
    o = o || {};
    var id = "rb-f" + (++uid);
    var control = o.rows
      ? el("textarea", { id: id, rows: String(o.rows), maxlength: String(o.max || 2000) })
      : el("input", { id: id, type: "text", maxlength: String(o.max || 200), autocomplete: o.autocomplete || "off" });
    if (o.placeholder) control.setAttribute("placeholder", o.placeholder);
    if (o.list) control.setAttribute("list", o.list);
    control.value = obj[key] == null ? "" : obj[key];
    control.addEventListener("input", function () { obj[key] = control.value; changed(); });
    var wrap = el("div", { class: "rb-field" + (o.wide ? " wide" : "") }, [el("label", { for: id, text: label }), control]);
    if (o.hint) wrap.appendChild(el("div", { class: "rb-hint", text: o.hint }));
    if (o.ai && AI) wrap.appendChild(aiBlock(o.ai, obj, key, control));
    return wrap;
  }
  function group(title, hint, kids) {
    var g = el("fieldset", { class: "rb-group" }, [el("legend", { text: title })]);
    if (hint) g.appendChild(el("p", { class: "rb-hint", text: hint }));
    kids.forEach(function (k) { g.appendChild(k); });
    return g;
  }
  function button(label, onClick, cls) {
    var b = el("button", { type: "button", class: cls || "ghost", text: label });
    b.addEventListener("click", onClick);
    return b;
  }
  function move(list, i, by) {
    var j = i + by;
    if (j < 0 || j >= list.length) return;
    var t = list[i]; list[i] = list[j]; list[j] = t;
    render(); changed();
  }
  // a repeating block: a card per item with move and remove buttons, and an add button
  function repeater(list, make, build, addLabel, max, itemName) {
    var box = el("div", { class: "rb-repeat" });
    list.forEach(function (item, i) {
      var tools = el("div", { class: "rb-tools" }, [
        button("Move up", function () { move(list, i, -1); }, "ghost sm"), button("Move down", function () { move(list, i, 1); }, "ghost sm"),
        button("Remove", function () { list.splice(i, 1); render(); changed(); }, "ghost sm rb-danger")
      ]);
      var card = el("div", { class: "rb-card" }, [el("div", { class: "rb-cardhead" }, [el("b", { text: itemName + " " + (i + 1) }), tools])]);
      build(item).forEach(function (f) { card.appendChild(f); });
      box.appendChild(card);
    });
    if (list.length < max) box.appendChild(button("+ " + addLabel, function () { list.push(make()); render(); changed(); }, "ghost"));
    return box;
  }


  // ------------------------------------------------------------------ AI help (optional: shown only when the server says it is available)
  function aiBlock(spec, obj, key, control) {
    var box = el("div", { class: "rb-aibox" });
    var btn = button(spec.kind === "summary" ? "Write with AI" : "Improve with AI", ask, "ghost sm");
    var panel = el("div", { class: "rb-ai" });
    panel.hidden = true;
    box.appendChild(el("div", { class: "rb-airow" }, [btn, el("span", { class: "rb-hint", text: "Sends this text to an AI service." })]));
    box.appendChild(panel);
    var asked = [];                      // the lines of the box when AI was asked, so a suggestion lands on the right line

    function close() { panel.hidden = true; panel.textContent = ""; }
    function put(v) { control.value = v; obj[key] = v; changed(); }
    function fail(message) {
      panel.textContent = ""; panel.hidden = false;
      panel.appendChild(el("div", { class: "rb-aierr", text: message }));
      panel.appendChild(el("div", { class: "rb-aiact" }, [button("Try again", ask, "ghost sm"), button("Dismiss", close, "ghost sm")]));
    }
    function warnings(item) {
      var w = el("div", { class: "rb-aiwarn" });
      var u = item.unsupported || [], p = item.placeholders || [];
      if (u.length) w.appendChild(el("div", { text: "Check this: " + u.join(", ") + (u.length === 1 ? " is" : " are") + " not in your document. Keep it only if it is true." }));
      if (p.length) w.appendChild(el("div", { text: "Replace " + p.join(" ") + " with a real detail, or delete it." }));
      return w.childNodes.length ? w : null;
    }
    function current() { return String(control.value || "").split("\n"); }
    function unchanged() { var c = current(); return c.length === asked.length && c.every(function (l, i) { return l === asked[i]; }); }
    function show(out) {
      panel.textContent = ""; panel.hidden = false;
      panel.appendChild(el("div", { class: "rb-aihead" }, [el("b", { text: "AI suggestions. " }), el("span", { class: "rb-hint", text: "AI can get things wrong: check every line before you use it." })]));
      if (spec.kind === "summary") {
        panel.appendChild(el("div", { class: "rb-aitext", text: out.suggestion }));
        var sw = warnings(out); if (sw) panel.appendChild(sw);
        panel.appendChild(el("div", { class: "rb-aiact" }, [button("Use this summary", function () { put(out.suggestion); close(); }, ""), button("Try again", ask, "ghost sm"), button("Dismiss", close, "ghost sm")]));
        return;
      }
      var lineAt = [];
      asked.forEach(function (l, i) { if (l.trim()) lineAt.push(i); });
      out.items.forEach(function (item, i) {
        var row = el("div", { class: "rb-aiitem" }, [el("div", { class: "rb-aiwas", text: "Was: " + item.original }), el("div", { class: "rb-aitext", text: item.suggestion })]);
        var w = warnings(item); if (w) row.appendChild(w);
        if (item.changed) {
          var use = button("Use this line", function () {
            if (!unchanged()) { fail("You changed this text after asking. Ask AI again."); return; }
            var c = current(); c[lineAt[i]] = item.suggestion; asked = c; put(c.join("\n"));
            use.disabled = true; use.textContent = "Used";
          }, "ghost sm");
          row.appendChild(use);
        } else row.appendChild(el("div", { class: "rb-hint", text: "No change suggested." }));
        panel.appendChild(row);
      });
      var any = out.items.some(function (it) { return it.changed; });
      var all = any ? button("Use all suggestions", function () {
        if (!unchanged()) { fail("You changed this text after asking. Ask AI again."); return; }
        var c = current();
        out.items.forEach(function (it, i) { if (it.changed) c[lineAt[i]] = it.suggestion; });
        put(c.join("\n")); close();
      }, "") : null;
      panel.appendChild(el("div", { class: "rb-aiact" }, [all, button("Try again", ask, "ghost sm"), button("Dismiss", close, "ghost sm")]));
    }
    function ask() {
      if (btn.disabled) return;
      var extra = { kind: spec.kind };
      if (spec.kind === "bullets") {
        extra.section = spec.section;
        extra.index = state[spec.section].indexOf(spec.item);
        if (extra.index < 0) return;
      }
      asked = current();
      btn.disabled = true; panel.hidden = false; panel.textContent = "";
      panel.appendChild(el("div", { class: "rb-aiwait", text: "Working on it. This can take up to a minute." }));
      post("/api/builder/ai", payload(extra)).then(function (res) {
        if (!res.ok) return readError(res).then(function (m) { throw new Error(m); });
        return res.json();
      }).then(show).catch(function (e) { fail(e.message || "AI help could not be reached."); })
        .then(function () { btn.disabled = false; });
    }
    return box;
  }

  // ------------------------------------------------------------------ the form
  function render() {
    formEl.textContent = "";
    uid = 0;                         // ids restart on every rebuild, so a field keeps the same id
    var basics = group("About you", "Only your name is needed to see a preview.", [
      el("div", { class: "rb-grid" }, [
        field("Full name", state, "name", { max: 80, autocomplete: "name", placeholder: "Jane Sample" }),
        field("Email", state.contact, "email", { max: 100, autocomplete: "email", placeholder: "jane@example.com" }),
        field("Phone", state.contact, "phone", { max: 40, autocomplete: "tel", placeholder: "+44 7700 900123" }),
        field("Location", state.contact, "location", { max: 100, placeholder: "City, Country" })
      ]),
      field("Links (one per line)", state.contact, "links", { rows: 2, max: 900, wide: true, placeholder: "https://linkedin.com/in/yourname", hint: "Include https:// to make a link clickable." })
    ]);
    var summary = group(CV ? "Profile" : "Summary", CV ? "Two to four sentences on who you are, your field and what you offer." : "Two or three sentences: your field, your strongest result and the kind of role you want.", [
      field(CV ? "Profile" : "Summary", state, "summary", { rows: 4, max: 1500, wide: true, ai: { kind: "summary" } })
    ]);
    var experience = group(CV ? "Work experience" : "Experience", "Newest first. Write one achievement per line, starting with what you did, and add numbers where you can.", [
      repeater(state.experience, job, function (e) {
        return [el("div", { class: "rb-grid" }, [
          field("Job title", e, "title", { max: 100 }), field("Company", e, "company", { max: 100 }), field("Location", e, "location", { max: 100 }),
          field("Start", e, "start", { max: 30, placeholder: "Mar 2022" }), field("End", e, "end", { max: 30, placeholder: "Present" })
        ]), field("What you did and achieved (one per line)", e, "bullets", { rows: 5, max: 6000, wide: true, ai: { kind: "bullets", section: "experience", item: e } })];
      }, "Add a job", 15, "Job")
    ]);
    var education = group("Education", null, [
      repeater(state.education, school, function (e) {
        return [el("div", { class: "rb-grid" }, [
          field("Degree or course", e, "degree", { max: 100 }), field("School", e, "school", { max: 100 }),
          field("Start", e, "start", { max: 30, placeholder: "2015" }), field("End", e, "end", { max: 30, placeholder: "2018" })
        ]), field("Details (one per line, optional)", e, "details", { rows: 2, max: 3000, wide: true, placeholder: CV ? "Thesis, supervisor, honours" : "Honours, relevant modules" })];
      }, "Add education", 8, "School")
    ]);
    var skills = group("Skills", "Group them if you like: a heading such as Languages, then the skills separated by commas.", [
      repeater(state.skills, function () { return { label: "", items: "" }; }, function (e) {
        return [el("div", { class: "rb-grid" }, [field("Heading (optional)", e, "label", { max: 40, placeholder: "Languages" }), field("Skills", e, "items", { max: 500, placeholder: "Ruby, Go, SQL" })])];
      }, "Add a skills group", 10, "Group")
    ]);
    var projects = group(CV ? "Projects and research" : "Projects", "Optional.", [
      repeater(state.projects, function () { return { title: "", link: "", start: "", end: "", bullets: "" }; }, function (e) {
        return [el("div", { class: "rb-grid" }, [field("Name", e, "title", { max: 100 }), field("Link", e, "link", { max: 200, placeholder: "https://" }),
          field("Start", e, "start", { max: 30 }), field("End", e, "end", { max: 30 })]), field("What it is (one point per line)", e, "bullets", { rows: 3, max: 6000, wide: true, ai: { kind: "bullets", section: "projects", item: e } })];
      }, "Add a project", 8, "Project")
    ]);
    var presets = el("datalist", { id: "rb-presets" }, ["Certifications", "Languages", "Awards", "Publications", "Research", "Teaching", "Conferences", "Volunteering", "Interests"].map(function (t) { return el("option", { value: t }); }));
    var extras = group("Extra sections", CV ? "Publications, research, teaching, languages and so on." : "Certifications, languages, awards and so on. Optional.", [
      presets,
      repeater(state.extras, function () { return { title: "", items: "" }; }, function (e) {
        return [field("Section title", e, "title", { max: 40, list: "rb-presets", placeholder: "Certifications" }), field("Items (one per line)", e, "items", { rows: 3, max: 9000, wide: true })];
      }, "Add a section", 8, "Section")
    ]);
    var parts = CV ? [basics, summary, education, experience, skills, projects, extras] : [basics, summary, experience, skills, education, projects, extras];
    if (CV) parts.push(group("References", "Many CVs end with a line like this.", [field("References", state, "references", { rows: 2, max: 600, wide: true })]));
    parts.forEach(function (p) { formEl.appendChild(p); });
  }

  // ------------------------------------------------------------------ preview and downloads
  function payload(extra) {
    var s = JSON.parse(JSON.stringify(state));
    s.contact.links = String(s.contact.links || "");
    var out = { mode: MODE, template: layout, paper: paper, data: s };
    Object.keys(extra || {}).forEach(function (k) { out[k] = extra[k]; });
    return JSON.stringify(out);
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body, credentials: "same-origin" });
  }
  function say(text, bad) { statusEl.textContent = text || ""; statusEl.className = "rb-status" + (bad ? " bad" : ""); }
  function readError(res) {
    return res.text().then(function (t) {
      try { var j = JSON.parse(t); return j.error || j.detail || "Something went wrong."; } catch (e) { return "Something went wrong. Please try again."; }
    });
  }
  function empty() { previewEl.innerHTML = '<div class="rb-empty">Type your name to see your ' + (CV ? "CV" : "résumé") + " here, or fill in an example to see how it looks.</div>"; }
  function preview() {
    if (!String(state.name || "").trim()) { previewSeq++; empty(); say(""); return; }
    var mine = ++previewSeq;
    post("/api/builder/preview", payload()).then(function (res) {
      if (mine !== previewSeq) return null;
      if (res.ok) return res.json().then(function (j) { if (mine === previewSeq) { previewEl.innerHTML = j.html; say(""); } });
      return readError(res).then(function (m) { if (mine === previewSeq) say(m, true); });
    }).catch(function () { if (mine === previewSeq) say("The preview could not be updated. Check your connection.", true); });
  }
  function changed() {
    store(KEY, JSON.stringify(state));
    clearTimeout(timer);
    timer = setTimeout(preview, 400);
  }
  function download(fmt, btn) {
    if (!String(state.name || "").trim()) { say("Add your name first.", true); return; }
    if (/\[ADD:/i.test(JSON.stringify(state)) && !window.confirm("Some text still has [ADD: ...] prompts that need a real detail. Download anyway?")) return;
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = "Making your file...";
    post("/api/builder/download", payload({ format: fmt })).then(function (res) {
      if (!res.ok) return readError(res).then(function (m) { throw new Error(m); });
      var m = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
      return res.blob().then(function (b) { return { blob: b, name: m ? m[1] : (CV ? "cv." : "resume.") + fmt }; });
    }).then(function (f) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(f.blob); a.download = f.name;
      document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
      say("Downloaded " + f.name + ".");
    }).catch(function (e) { say(e.message || "Could not make the file.", true); })
      .then(function () { btn.disabled = false; btn.textContent = old; });
  }

  // ------------------------------------------------------------------ the controls above the preview
  function setLayout(name) {
    layout = name; store(LAYOUT_KEY, name);
    Array.prototype.forEach.call(document.querySelectorAll("[data-layout]"), function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-layout") === name ? "true" : "false"); });
    clearTimeout(timer); preview();
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-layout]"), function (b) { b.addEventListener("click", function () { setLayout(b.getAttribute("data-layout")); }); });
  document.getElementById("rb-paper").addEventListener("change", function (e) { paper = e.target.value === "letter" ? "letter" : "a4"; });
  Array.prototype.forEach.call(document.querySelectorAll("[data-download]"), function (b) { b.addEventListener("click", function () { download(b.getAttribute("data-download"), b); }); });
  document.getElementById("rb-example").addEventListener("click", function () {
    if (String(state.name || "").trim() && !window.confirm("Replace what you have written with the example?")) return;
    state = fromSample(); render(); changed();
  });
  document.getElementById("rb-clear").addEventListener("click", function () {
    if (!window.confirm("Clear everything you have written? This cannot be undone.")) return;
    state = blank(); store(KEY, null); render(); preview();
  });

  formEl.addEventListener("submit", function (e) { e.preventDefault(); });   // Enter in a box must not reload the page
  render();
  setLayout(layout);
})();
