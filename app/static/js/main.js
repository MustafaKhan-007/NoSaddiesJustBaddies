/* Bloom Anyway — public site JS (vanilla, no dependencies) */
(function () {
  "use strict";

  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- hero load-in (one page-load moment only) ---- */
  var hero = document.querySelector(".hero");
  if (hero) {
    if (reducedMotion) {
      hero.classList.add("loaded");
    } else {
      requestAnimationFrame(function () { hero.classList.add("loaded"); });
    }
  }

  /* ---- scroll-triggered reveal ---- */
  var revealEls = document.querySelectorAll(".reveal");
  if (revealEls.length && !reducedMotion && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    revealEls.forEach(function (el) { observer.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("visible"); });
  }

  /* ---- mobile nav drawer (accessible, focus-trapped) ---- */
  var toggle = document.querySelector(".nav-toggle");
  var drawer = document.getElementById("nav-drawer");
  if (toggle && drawer) {
    var focusables = function () {
      return drawer.querySelectorAll("a[href], button:not([disabled])");
    };
    var close = function () {
      drawer.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
      toggle.focus();
    };
    toggle.addEventListener("click", function () {
      var open = drawer.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var first = focusables()[0];
        if (first) first.focus();
      }
    });
    document.addEventListener("keydown", function (e) {
      if (!drawer.classList.contains("open")) return;
      if (e.key === "Escape") { close(); return; }
      if (e.key !== "Tab") return;
      var items = focusables();
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        toggle.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        toggle.focus();
      } else if (!e.shiftKey && document.activeElement === toggle) {
        e.preventDefault();
        first.focus();
      }
    });
  }

  /* ---- password show/hide toggles ---- */
  document.querySelectorAll(".password-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.dataset.toggles);
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  /* ---- confirm dialogs (delete account etc.) ---- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  /* ---- live preview when picking a new avatar ---- */
  document.querySelectorAll("[data-avatar-preview]").forEach(function (input) {
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) return;
      var pick = input.closest(".avatar-edit") &&
                 input.closest(".avatar-edit").querySelector(".avatar");
      if (!pick) return;
      var url = URL.createObjectURL(file);
      pick.style.backgroundImage = "url('" + url + "')";
      pick.textContent = "";
      // untick "remove" if the person just chose a new picture
      var remove = document.querySelector("input[name='remove_avatar']");
      if (remove) remove.checked = false;
    });
  });

  /* ---- marketplace listing form: show location box for services ---- */
  var listingForm = document.getElementById("listing-form");
  if (listingForm) {
    var locBox = listingForm.querySelector("[data-location-box]");
    var locInput = listingForm.querySelector("#location");
    var syncKind = function () {
      var picked = listingForm.querySelector('input[name="kind"]:checked');
      var isService = !!(picked && picked.value === "service");
      listingForm.classList.toggle("is-service", isService);
      listingForm.classList.toggle("is-product", !isService);
      if (locBox) {
        if (isService) locBox.removeAttribute("hidden");
        else locBox.setAttribute("hidden", "");
      }
      if (locInput) {
        locInput.required = isService;
        if (!isService) locInput.value = locInput.value; // keep typed text if they toggle back
      }
    };
    listingForm.querySelectorAll('input[name="kind"]').forEach(function (r) {
      r.addEventListener("change", syncKind);
      // also catch clicks on the visible label chip
      var label = r.closest("label");
      if (label) label.addEventListener("click", function () {
        // let the radio update, then sync on next tick
        setTimeout(syncKind, 0);
      });
    });
    syncKind();

    var max = parseInt(listingForm.getAttribute("data-tag-max") || "24", 10);
    var boxes = listingForm.querySelectorAll('input[name="tags"]');
    var countEl = listingForm.querySelector("[data-tag-count]");
    var syncTags = function () {
      var n = 0;
      boxes.forEach(function (b) { if (b.checked) n++; });
      if (countEl) countEl.textContent = n + " / " + max + " selected";
      boxes.forEach(function (b) {
        if (!b.checked) b.disabled = n >= max;
      });
    };
    boxes.forEach(function (b) { b.addEventListener("change", syncTags); });
    syncTags();
  }

  /* ---- Lemon Squeezy overlay (re-init if lemon.js loaded after us) ---- */
  if (window.createLemonSqueezy) {
    window.createLemonSqueezy();
  } else {
    document.querySelectorAll("script[src*='lemon.js']").forEach(function (s) {
      s.addEventListener("load", function () {
        if (window.createLemonSqueezy) window.createLemonSqueezy();
      });
    });
  }

  /* ---- Coaching fold: smooth expand on My space ---- */
  document.querySelectorAll("[data-coaching-toggle]").forEach(function (btn) {
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    btn.addEventListener("click", function () {
      var open = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", open ? "false" : "true");
      btn.classList.toggle("is-open", !open);
      panel.setAttribute("aria-hidden", open ? "true" : "false");
      if (open) {
        panel.style.maxHeight = panel.scrollHeight + "px";
        requestAnimationFrame(function () {
          panel.style.maxHeight = "0px";
          panel.classList.remove("is-open");
        });
      } else {
        panel.classList.add("is-open");
        panel.style.maxHeight = "0px";
        requestAnimationFrame(function () {
          panel.style.maxHeight = panel.scrollHeight + "px";
        });
      }
    });
  });

  /* ---- Showcase listing gallery: thumbnails swap the hero image ---- */
  document.querySelectorAll("[data-listing-gallery]").forEach(function (gallery) {
    var hero = gallery.querySelector("#listing-hero") ||
               gallery.querySelector(".listing-detail__hero");
    if (!hero) return;
    gallery.querySelectorAll("[data-listing-thumb]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var src = btn.getAttribute("data-src");
        if (!src) return;
        hero.src = src;
        gallery.querySelectorAll("[data-listing-thumb]").forEach(function (other) {
          other.classList.toggle("is-active", other === btn);
          other.setAttribute("aria-pressed", other === btn ? "true" : "false");
        });
      });
    });
  });
})();
