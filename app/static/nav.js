// Header menus are plain <details> elements; this only closes them when you click elsewhere or press Escape,
// and folds the mobile menu away after a link is chosen.
(function () {
  var menus = function () { return document.querySelectorAll("header.top details.menu[open]"); };
  document.addEventListener("click", function (e) {
    menus().forEach(function (m) { if (!m.contains(e.target)) m.removeAttribute("open"); });
    var toggle = document.getElementById("nav-toggle");
    if (toggle && toggle.checked && e.target.closest && e.target.closest(".nav-links a, .nav-actions a")) toggle.checked = false;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    menus().forEach(function (m) { m.removeAttribute("open"); var s = m.querySelector("summary"); if (s) s.focus(); });
    var toggle = document.getElementById("nav-toggle");
    if (toggle) toggle.checked = false;
  });
})();
