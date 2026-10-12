(function setupImportProgress() {
  const form = document.querySelector(".import-form");
  const progressPanel = document.querySelector("[data-import-progress]");
  if (!form || !progressPanel) return;

  const progressBar = progressPanel.querySelector("progress");
  const status = progressPanel.querySelector(".import-progress-status");
  const detail = progressPanel.querySelector(".import-progress-detail");
  const percentOutput = progressPanel.querySelector("[data-import-percent]");
  const steps = [...progressPanel.querySelectorAll("[data-import-step]")];
  const submitButton = form.querySelector('button[type="submit"]');
  const label = (name) => progressPanel.dataset[name];
  const number = (value) => new Intl.NumberFormat().format(value);
  const stepOrder = ["upload", "count", "process", "complete"];

  function setStep(activeStep) {
    const activeIndex = stepOrder.indexOf(activeStep);
    steps.forEach((step) => {
      const stepIndex = stepOrder.indexOf(step.dataset.importStep);
      step.classList.toggle("is-complete", stepIndex < activeIndex);
      step.classList.toggle("is-active", stepIndex === activeIndex);
      if (stepIndex === activeIndex) {
        step.setAttribute("aria-current", "step");
      } else {
        step.removeAttribute("aria-current");
      }
    });
  }

  function setPercent(value) {
    const percent = Math.max(0, Math.min(100, value));
    progressBar.value = percent;
    progressBar.setAttribute("aria-valuenow", String(Math.round(percent)));
    progressBar.setAttribute("aria-valuetext", `${Math.round(percent)}%`);
    percentOutput.textContent = `${Math.floor(percent)}%`;
  }

  function setIndeterminate() {
    progressBar.removeAttribute("value");
    progressBar.removeAttribute("aria-valuenow");
    progressBar.setAttribute("aria-valuetext", label("countingLabel"));
    percentOutput.textContent = "…";
  }

  function showError(message) {
    status.textContent = label("failedLabel");
    detail.textContent = message;
    form.setAttribute("aria-busy", "false");
    if (submitButton) submitButton.disabled = false;
  }

  function handleEvent(event, xhr) {
    if (event.type === "phase" && event.phase === "counting") {
      setStep("count");
      status.textContent = label("countingLabel");
      detail.textContent = "";
      setIndeterminate();
      return;
    }

    if (event.type === "count_progress") {
      detail.textContent = `${number(event.files_scanned)} / ${number(event.total_files)} ${label("scannedLabel")}`;
      return;
    }

    if (event.type === "progress") {
      setStep("process");
      status.textContent = label("processingLabel");
      if (event.total > 0) {
        setPercent(event.processed / event.total * 100);
      } else {
        setIndeterminate();
      }
      const fileProgress = event.total_files
        ? ` · ${label("fileLabel")} ${number(event.file_number)} / ${number(event.total_files)}`
        : "";
      detail.textContent = `${number(event.processed)} / ${number(event.total)} ${label("linesLabel")}${fileProgress}`;
      return;
    }

    if (event.type === "error") {
      showError(event.message);
      xhr.abort();
      return;
    }

    if (event.type === "done") {
      setStep("complete");
      setPercent(100);
      const failed = Number(event.failed || 0);
      status.textContent = failed ? label("failedLabel") : label("completeLabel");
      detail.textContent = `${number(event.created)} ${label("createdLabel")}; ${number(event.skipped)} ${label("skippedLabel")}; ${number(failed)} ${label("failedCountLabel")}; ${number(event.files)} ${label("filesCompleteLabel")}`;
      form.setAttribute("aria-busy", "false");
      window.setTimeout(() => window.location.assign(event.redirect), 900);
    }
  }

  form.addEventListener("submit", (submitEvent) => {
    submitEvent.preventDefault();
    progressPanel.hidden = false;
    setStep("upload");
    setPercent(0);
    status.textContent = label("uploadingLabel");
    detail.textContent = `${label("uploadDetail")}: 0%`;
    form.setAttribute("aria-busy", "true");
    if (submitButton) submitButton.disabled = true;

    const xhr = new XMLHttpRequest();
    let responseOffset = 0;
    let responseBuffer = "";

    function readEvents() {
      const nextText = xhr.responseText.slice(responseOffset);
      responseOffset = xhr.responseText.length;
      responseBuffer += nextText;
      const lines = responseBuffer.split("\n");
      responseBuffer = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line), xhr);
        } catch {
          showError(label("failedLabel"));
          xhr.abort();
          return;
        }
      }
    }

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = event.loaded / event.total * 100;
      setPercent(percent);
      detail.textContent = `${label("uploadDetail")}: ${Math.floor(percent)}%`;
    });
    xhr.upload.addEventListener("load", () => {
      setStep("count");
      status.textContent = label("countingLabel");
      detail.textContent = "";
      setIndeterminate();
    });
    xhr.addEventListener("progress", readEvents);
    xhr.addEventListener("load", () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        let message = label("failedLabel");
        try {
          message = JSON.parse(xhr.responseText).error || message;
        } catch {
          // Keep the localized fallback for unexpected server responses.
        }
        showError(message);
        return;
      }
      readEvents();
      if (responseBuffer.trim()) {
        try {
          handleEvent(JSON.parse(responseBuffer), xhr);
        } catch {
          showError(label("failedLabel"));
        }
      }
    });
    xhr.addEventListener("error", () => showError(label("failedLabel")));
    xhr.addEventListener("abort", () => {
      if (form.getAttribute("aria-busy") === "true") {
        showError(label("failedLabel"));
      }
    });
    xhr.open("POST", form.action || window.location.href);
    xhr.setRequestHeader("X-Import-Progress", "1");
    xhr.send(new FormData(form));
  });
})();

// Account form: show only the identifier field that matches the
// selected account type (email -> Email, user -> Username).
(function setupAccountTypeToggle() {
  const form = document.querySelector("#account-form");
  if (!form) return;
  const typeSelect = form.querySelector("#id_account_type");
  if (!typeSelect) return;

  const emailField = form.querySelector("#id_email_name")?.closest(".field");
  const userField = form.querySelector("#id_user_name")?.closest(".field");

  function applyToggle() {
    const isEmail = typeSelect.value === "email";
    if (emailField) emailField.style.display = isEmail ? "" : "none";
    if (userField) userField.style.display = isEmail ? "none" : "";
  }

  typeSelect.addEventListener("change", applyToggle);
  applyToggle();
})();

// Searchable and selectable drop-down comboboxes for all Add/Edit Record fields.
(function setupSearchableComboboxes() {
  const wraps = document.querySelectorAll("[data-combobox-wrap]");
  if (!wraps.length) return;

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function highlightMatch(text, query) {
    if (!query) return escapeHtml(text);
    const lowerText = text.toLowerCase();
    const lowerQuery = query.toLowerCase();
    const idx = lowerText.indexOf(lowerQuery);
    if (idx === -1) return escapeHtml(text);
    const before = escapeHtml(text.slice(0, idx));
    const match = escapeHtml(text.slice(idx, idx + query.length));
    const after = escapeHtml(text.slice(idx + query.length));
    return `${before}<mark class="combobox-mark">${match}</mark>${after}`;
  }

  function closeAllExcept(activeWrap) {
    wraps.forEach((w) => {
      if (w !== activeWrap) {
        w.classList.remove("is-open");
        const menu = w.querySelector(".combobox-dropdown");
        if (menu) menu.hidden = true;
      }
    });
  }

  wraps.forEach((wrap) => {
    const dropdown = wrap.querySelector(".combobox-dropdown");
    const toggleBtn = wrap.querySelector(".combobox-toggle");
    const textInput = wrap.querySelector("input[data-combobox]");
    const selectEl = wrap.querySelector("select[data-combobox-select]");

    if (!dropdown) return;

    // Case 1: Reference text input combobox (URL, Email, Username, Password, IP)
    if (textInput) {
      const kind = textInput.dataset.combobox;
      const listId = textInput.dataset.listId;
      const knownSet = new Set();
      const allOptions = [];

      if (listId) {
        const datalist = document.getElementById(listId);
        datalist?.querySelectorAll("option").forEach((opt) => {
          const val = (opt.value || "").trim();
          if (val && !knownSet.has(val)) {
            knownSet.add(val);
            allOptions.push(val);
          }
        });
      }

      let activeIndex = -1;
      let fetchTimer = null;

      function mergeOptions(values) {
        values.forEach((val) => {
          const trimmed = (val || "").trim();
          if (trimmed && !knownSet.has(trimmed)) {
            knownSet.add(trimmed);
            allOptions.push(trimmed);
          }
        });
      }

      function renderOptions(filterQuery) {
        const q = (filterQuery ?? "").trim();
        const qLower = q.toLowerCase();
        const currentVal = textInput.value.trim();

        const filtered = qLower
          ? allOptions.filter((item) => item.toLowerCase().includes(qLower))
          : allOptions.slice(0, 100);

        const items = [];
        let exactExists = false;

        filtered.slice(0, 100).forEach((item) => {
          if (item.toLowerCase() === qLower) exactExists = true;
          const isSelected = item === currentVal;
          items.push({
            value: item,
            labelHtml: highlightMatch(item, q),
            isSelected,
            isNew: false,
          });
        });

        if (q && !exactExists) {
          items.unshift({
            value: q,
            labelHtml: `<span class="combobox-new-tag">+</span> ${escapeHtml(q)}`,
            isSelected: false,
            isNew: true,
          });
        }

        dropdown.innerHTML = "";
        activeIndex = -1;

        if (!items.length) {
          const empty = document.createElement("div");
          empty.className = "combobox-empty";
          empty.textContent = "Type a value to filter or add";
          dropdown.appendChild(empty);
          return;
        }

        items.forEach((item, index) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className =
            "combobox-option" +
            (item.isSelected ? " is-selected" : "") +
            (item.isNew ? " is-new" : "");
          btn.setAttribute("role", "option");
          btn.setAttribute("aria-selected", String(item.isSelected));
          btn.dataset.value = item.value;
          btn.innerHTML = `<span class="combobox-option-text" dir="auto">${item.labelHtml}</span>${
            item.isSelected ? '<span class="combobox-check" aria-hidden="true">✓</span>' : ""
          }`;

          btn.addEventListener("mousedown", (e) => {
            e.preventDefault();
            selectOption(item.value);
          });

          btn.addEventListener("mouseenter", () => {
            setActiveOption(index);
          });

          dropdown.appendChild(btn);
        });
      }

      function setActiveOption(idx) {
        const btns = dropdown.querySelectorAll(".combobox-option");
        btns.forEach((b, i) => {
          b.classList.toggle("is-active", i === idx);
        });
        activeIndex = idx;
        if (idx >= 0 && btns[idx]) {
          btns[idx].scrollIntoView({ block: "nearest" });
        }
      }

      function openDropdown(filterQuery) {
        closeAllExcept(wrap);
        renderOptions(filterQuery);
        wrap.classList.add("is-open");
        dropdown.hidden = false;
      }

      function closeDropdown() {
        wrap.classList.remove("is-open");
        dropdown.hidden = true;
        activeIndex = -1;
      }

      function selectOption(val) {
        textInput.value = val;
        if (val && !knownSet.has(val)) {
          knownSet.add(val);
          allOptions.unshift(val);
        }
        closeDropdown();
        textInput.dispatchEvent(new Event("change", { bubbles: true }));
      }

      function fetchSuggestions(q) {
        if (!kind) return;
        if (fetchTimer) clearTimeout(fetchTimer);
        fetchTimer = setTimeout(() => {
          fetch(`/references/${encodeURIComponent(kind)}/suggest/?q=${encodeURIComponent(q)}`, {
            headers: { Accept: "application/json" },
          })
            .then((res) => (res.ok ? res.json() : null))
            .then((data) => {
              if (data && Array.isArray(data.results)) {
                mergeOptions(data.results);
                if (!dropdown.hidden) {
                  renderOptions(textInput.value);
                }
              }
            })
            .catch(() => {});
        }, 120);
      }

      textInput.addEventListener("focus", () => {
        openDropdown("");
        fetchSuggestions(textInput.value.trim());
      });

      textInput.addEventListener("click", () => {
        if (dropdown.hidden) {
          openDropdown("");
        }
      });

      textInput.addEventListener("input", () => {
        openDropdown(textInput.value);
        fetchSuggestions(textInput.value.trim());
      });

      textInput.addEventListener("keydown", (e) => {
        const btns = dropdown.querySelectorAll(".combobox-option");
        if (e.key === "ArrowDown") {
          e.preventDefault();
          if (dropdown.hidden) {
            openDropdown("");
          } else if (btns.length) {
            setActiveOption((activeIndex + 1) % btns.length);
          }
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          if (dropdown.hidden) {
            openDropdown("");
          } else if (btns.length) {
            setActiveOption((activeIndex - 1 + btns.length) % btns.length);
          }
        } else if (e.key === "Enter") {
          if (!dropdown.hidden && activeIndex >= 0 && btns[activeIndex]) {
            e.preventDefault();
            selectOption(btns[activeIndex].dataset.value);
          } else if (!dropdown.hidden) {
            closeDropdown();
          }
        } else if (e.key === "Escape") {
          closeDropdown();
        }
      });

      toggleBtn?.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (dropdown.hidden) {
          textInput.focus();
          openDropdown("");
          fetchSuggestions("");
        } else {
          closeDropdown();
        }
      });
    }

    // Case 2: Select element combobox (Account type)
    if (selectEl) {
      const options = Array.from(selectEl.options).map((opt) => ({
        value: opt.value,
        label: opt.textContent.trim(),
      }));

      const filterInput = document.createElement("input");
      filterInput.type = "text";
      filterInput.className = "form-control combobox-input";
      filterInput.autocomplete = "off";
      filterInput.setAttribute("aria-label", selectEl.getAttribute("aria-label") || "");

      function syncDisplayFromSelect() {
        const selectedOpt = options.find((o) => o.value === selectEl.value) || options[0];
        if (selectedOpt) {
          filterInput.value = selectedOpt.label;
        }
      }

      syncDisplayFromSelect();
      selectEl.classList.add("combobox-native-select");
      wrap.insertBefore(filterInput, selectEl);

      let activeIndex = -1;

      function renderSelectOptions(query) {
        const q = (query || "").trim().toLowerCase();
        const filtered = q
          ? options.filter((o) => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q))
          : options;

        dropdown.innerHTML = "";
        activeIndex = -1;

        filtered.forEach((opt, idx) => {
          const isSelected = opt.value === selectEl.value;
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "combobox-option" + (isSelected ? " is-selected" : "");
          btn.setAttribute("role", "option");
          btn.setAttribute("aria-selected", String(isSelected));
          btn.dataset.value = opt.value;
          btn.innerHTML = `<span class="combobox-option-text">${highlightMatch(opt.label, query)}</span>${
            isSelected ? '<span class="combobox-check" aria-hidden="true">✓</span>' : ""
          }`;

          btn.addEventListener("mousedown", (e) => {
            e.preventDefault();
            chooseSelectOption(opt.value);
          });

          btn.addEventListener("mouseenter", () => {
            const btns = dropdown.querySelectorAll(".combobox-option");
            btns.forEach((b, i) => b.classList.toggle("is-active", i === idx));
            activeIndex = idx;
          });

          dropdown.appendChild(btn);
        });
      }

      function chooseSelectOption(val) {
        selectEl.value = val;
        syncDisplayFromSelect();
        wrap.classList.remove("is-open");
        dropdown.hidden = true;
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
      }

      function openSelectDropdown(q) {
        closeAllExcept(wrap);
        renderSelectOptions(q);
        wrap.classList.add("is-open");
        dropdown.hidden = false;
      }

      function closeSelectDropdown() {
        wrap.classList.remove("is-open");
        dropdown.hidden = true;
        syncDisplayFromSelect();
      }

      filterInput.addEventListener("focus", () => {
        filterInput.select();
        openSelectDropdown("");
      });

      filterInput.addEventListener("click", () => {
        if (dropdown.hidden) openSelectDropdown("");
      });

      filterInput.addEventListener("input", () => {
        openSelectDropdown(filterInput.value);
      });

      filterInput.addEventListener("blur", () => {
        setTimeout(() => {
          if (!wrap.contains(document.activeElement)) {
            closeSelectDropdown();
          }
        }, 120);
      });

      filterInput.addEventListener("keydown", (e) => {
        const btns = dropdown.querySelectorAll(".combobox-option");
        if (e.key === "ArrowDown") {
          e.preventDefault();
          if (dropdown.hidden) openSelectDropdown("");
          else if (btns.length) {
            activeIndex = (activeIndex + 1) % btns.length;
            btns.forEach((b, i) => b.classList.toggle("is-active", i === activeIndex));
          }
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          if (dropdown.hidden) openSelectDropdown("");
          else if (btns.length) {
            activeIndex = (activeIndex - 1 + btns.length) % btns.length;
            btns.forEach((b, i) => b.classList.toggle("is-active", i === activeIndex));
          }
        } else if (e.key === "Enter") {
          e.preventDefault();
          if (!dropdown.hidden && activeIndex >= 0 && btns[activeIndex]) {
            chooseSelectOption(btns[activeIndex].dataset.value);
          } else if (!dropdown.hidden && btns.length === 1) {
            chooseSelectOption(btns[0].dataset.value);
          }
        } else if (e.key === "Escape") {
          closeSelectDropdown();
        }
      });

      toggleBtn?.addEventListener("mousedown", (e) => {
        e.preventDefault();
        if (dropdown.hidden) {
          filterInput.focus();
          openSelectDropdown("");
        } else {
          closeSelectDropdown();
        }
      });
    }
  });

  document.addEventListener("mousedown", (e) => {
    wraps.forEach((w) => {
      if (!w.contains(e.target)) {
        w.classList.remove("is-open");
        const menu = w.querySelector(".combobox-dropdown");
        if (menu) menu.hidden = true;
      }
    });
  });
})();

// Click-to-sort column headers for sortable tables.
(function setupSortableTables() {
  const tables = document.querySelectorAll(".sortable-table");
  if (!tables.length) return;

  // Sort mapping: column key -> Django queryset order_by parameter
  const SORT_MAP = {
    id: { asc: "id", desc: "-id" },
    url: { asc: "url", desc: "-url" },
    account_type: { asc: "account_type", desc: "-account_type" },
    identifier: { asc: "identifier", desc: "-identifier" },
    ip: { asc: "ip", desc: "-ip" },
  };

  // Read current sort from URL params
  function getCurrentSort() {
    const params = new URLSearchParams(window.location.search);
    return params.get("sort") || "newest";
  }

  // Read the base URL for the table (without sort/page params)
  function getBaseUrl(table) {
    return table.dataset.sortBase || window.location.pathname + window.location.search;
  }

  tables.forEach((table) => {
    const headers = table.querySelectorAll("th.sortable");
    const currentSort = getCurrentSort();
    const baseUrl = getBaseUrl(table);

    headers.forEach((th) => {
      const col = th.dataset.sortCol;
      const indicator = th.querySelector(".sort-indicator");
      const mapping = SORT_MAP[col];
      if (!mapping) return;

      // Mark the currently sorted column
      if (mapping.asc === currentSort || currentSort === mapping.asc) {
        th.classList.add("is-sorted-asc");
        if (indicator) indicator.textContent = "▲";
        th.setAttribute("aria-sort", "ascending");
      } else if (mapping.desc === currentSort || "-" + mapping.asc === currentSort) {
        th.classList.add("is-sorted-desc");
        if (indicator) indicator.textContent = "▼";
        th.setAttribute("aria-sort", "descending");
      }

      const control = th.querySelector(".sort-control") || th;
      control.addEventListener("click", () => {
        const params = new URLSearchParams(
          baseUrl.includes("?") ? baseUrl.split("?")[1] : ""
        );
        const basePath = baseUrl.split("?")[0];

        // Toggle direction for this typed, server-side sort key.
        if (th.classList.contains("is-sorted-asc")) {
          params.set("sort", mapping.desc);
        } else {
          params.set("sort", mapping.asc);
        }
        params.delete("page");
        window.location.href = basePath + "?" + params.toString();
      });

      if (control !== th) {
        const label = control.dataset.sortLabel || control.textContent.trim();
        control.setAttribute(
          "aria-label",
          `${label}, ${th.classList.contains("is-sorted-asc") ? "currently ascending; activate to sort descending" : "activate to sort ascending"}`
        );
      } else {
        // Legacy markup stays keyboard operable, but the th retains its
        // native columnheader role (a header is not also a button role).
        th.setAttribute("tabindex", "0");
        th.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            th.click();
          }
        });
      }
    });
  });
})();
