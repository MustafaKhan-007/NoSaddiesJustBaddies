/* First Light — admin panel JS */
(function () {
  "use strict";

  /* ---- confirm destructive actions ---- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  /* ---- slug auto-fill from title (until slug is edited by hand) ---- */
  var title = document.getElementById("title");
  var slug = document.getElementById("slug");
  if (title && slug) {
    var slugTouched = slug.value !== "";
    slug.addEventListener("input", function () { slugTouched = true; });
    title.addEventListener("input", function () {
      if (slugTouched) return;
      slug.value = title.value.toLowerCase()
        .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 150);
    });
  }

  /* ---- live cover image preview ---- */
  var cover = document.getElementById("cover_url");
  var preview = document.getElementById("cover-preview");
  if (cover && preview) {
    var update = function () {
      if (cover.value.indexOf("https://") === 0) {
        preview.src = cover.value;
        preview.hidden = false;
      } else {
        preview.hidden = true;
      }
    };
    cover.addEventListener("change", update);
    update();
  }

  /* ---- curriculum repeatable rows ---- */
  var addModule = document.getElementById("add-module");
  var moduleList = document.getElementById("module-list");
  if (addModule && moduleList) {
    addModule.addEventListener("click", function () {
      var row = document.createElement("div");
      row.className = "form-row module-row";
      row.innerHTML =
        '<div class="field"><input type="text" name="curriculum_title" placeholder="Module title"></div>' +
        '<div class="field" style="display:flex;gap:8px;">' +
        '<input type="text" name="curriculum_desc" placeholder="Short description">' +
        '<button type="button" class="btn btn--secondary btn--sm remove-module" aria-label="Remove module">&times;</button></div>';
      moduleList.appendChild(row);
    });
    moduleList.addEventListener("click", function (e) {
      if (e.target.classList.contains("remove-module")) {
        e.target.closest(".module-row").remove();
      }
    });
  }

  /* ---- drag-handle reordering of the products table ---- */
  var tbody = document.getElementById("sortable-products");
  if (tbody) {
    var dragging = null;
    tbody.querySelectorAll("tr").forEach(function (row) {
      var handle = row.querySelector(".drag-handle");
      if (!handle) return;
      handle.addEventListener("mousedown", function () { row.draggable = true; });
      row.addEventListener("dragstart", function () {
        dragging = row;
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", function () {
        row.classList.remove("dragging");
        row.draggable = false;
        dragging = null;
        saveOrder();
      });
      row.addEventListener("dragover", function (e) {
        e.preventDefault();
        if (!dragging || dragging === row) return;
        var rect = row.getBoundingClientRect();
        var after = e.clientY > rect.top + rect.height / 2;
        tbody.insertBefore(dragging, after ? row.nextSibling : row);
      });
    });

    var saveOrder = function () {
      var ids = Array.prototype.map.call(
        tbody.querySelectorAll("tr[data-id]"),
        function (row) { return row.dataset.id; }
      );
      fetch(tbody.dataset.reorderUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": tbody.dataset.csrf
        },
        body: JSON.stringify({ ids: ids })
      });
    };
  }

  /* ---- collapsible long tables (show a few rows, expand on demand) ---- */
  document.querySelectorAll("table[data-collapsible]").forEach(function (table) {
    var limit = parseInt(table.getAttribute("data-collapsible"), 10) || 10;
    var body = table.tBodies[0];
    if (!body) return;
    var rows = Array.prototype.slice.call(body.rows);
    if (rows.length <= limit) return;

    var hidden = rows.slice(limit);
    var collapse = function () {
      hidden.forEach(function (r) { r.hidden = true; });
    };
    collapse();

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn--secondary btn--sm show-all-btn";
    var setLabel = function (expanded) {
      btn.textContent = expanded
        ? "Show fewer"
        : "Show all " + rows.length;
    };
    setLabel(false);
    btn.addEventListener("click", function () {
      var expanded = hidden[0] && hidden[0].hidden;
      hidden.forEach(function (r) { r.hidden = !expanded; });
      setLabel(expanded);
    });
    table.insertAdjacentElement("afterend", btn);
  });

  /* ---- dashboard charts (Chart.js from CDN) ---- */
  var dataEl = document.getElementById("dashboard-data");
  if (dataEl && window.Chart) {
    var data = JSON.parse(dataEl.textContent);
    var plum = "#7A2E62", rose = "#E08A6D", gold = "#EFA733";

    var revenueCtx = document.getElementById("chart-revenue");
    if (revenueCtx) {
      new Chart(revenueCtx, {
        type: "line",
        data: {
          labels: data.revenue.labels,
          datasets: [{
            label: "Revenue",
            data: data.revenue.values,
            borderColor: gold,
            backgroundColor: "rgba(239, 167, 51, 0.15)",
            fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2
          }]
        },
        options: {
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { maxTicksLimit: 9 } } }
        }
      });
    }

    var productsCtx = document.getElementById("chart-products");
    if (productsCtx) {
      new Chart(productsCtx, {
        type: "bar",
        data: {
          labels: data.products.labels,
          datasets: [{ label: "Orders", data: data.products.values, backgroundColor: rose }]
        },
        options: { indexAxis: "y", plugins: { legend: { display: false } } }
      });
    }

    var signupsCtx = document.getElementById("chart-signups");
    if (signupsCtx) {
      new Chart(signupsCtx, {
        type: "line",
        data: {
          labels: data.signups.labels,
          datasets: [
            { label: "Accounts", data: data.signups.users, borderColor: plum, tension: 0.3, borderWidth: 2 },
            { label: "Subscribers", data: data.signups.subscribers, borderColor: rose, tension: 0.3, borderWidth: 2 }
          ]
        }
      });
    }
  }
})();
