/* AI advice for the résumé and job match checker. Optional: the page works without it. */
(function () {
  "use strict";
  var box = document.getElementById("tm-ai");
  var btn = document.getElementById("tm-ai-btn");
  var out = document.getElementById("tm-ai-out");
  var resume = document.getElementById("resume");
  var job = document.getElementById("job");
  if (!box || !btn || !out || !resume || !job) return;

  function say(text, bad) {
    out.textContent = "";
    var p = document.createElement("p");
    p.textContent = text;
    if (bad) p.style.color = "#b91c1c";
    out.appendChild(p);
  }
  function show(tips) {
    out.textContent = "";
    if (!tips.length) { say("AI had no specific advice for these gaps."); return; }
    tips.forEach(function (t) {
      var d = document.createElement("div");
      d.className = "tm-tip";
      var b = document.createElement("b");
      b.textContent = t.keyword + ": ";
      d.appendChild(b);
      d.appendChild(document.createTextNode(t.advice));
      out.appendChild(d);
    });
    var note = document.createElement("p");
    note.className = "tl-hint";
    note.textContent = "AI can get things wrong. Only add what is true for you.";
    out.appendChild(note);
  }
  function readError(res) {
    return res.text().then(function (t) {
      try { var j = JSON.parse(t); return j.error || j.detail || "Something went wrong."; } catch (e) { return "Something went wrong. Please try again."; }
    });
  }
  btn.addEventListener("click", function () {
    if (btn.disabled) return;
    btn.disabled = true;
    say("Working on it. This can take up to a minute.");
    fetch(box.getAttribute("data-ai-url"), {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
      body: JSON.stringify({ resume: resume.value, job: job.value })
    }).then(function (res) {
      if (!res.ok) return readError(res).then(function (m) { throw new Error(m); });
      return res.json();
    }).then(function (j) { show(j.tips || []); })
      .catch(function (e) { say(e.message || "AI help could not be reached.", true); })
      .then(function () { btn.disabled = false; });
  });
})();
