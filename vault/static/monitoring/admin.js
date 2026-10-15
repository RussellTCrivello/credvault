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
    if (event.type === "job") {
      progressPanel._addLiveImportJob?.(event);
      return;
    }

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
        ? ` · ${number(event.files_completed)} / ${number(event.total_files)} ${label("filesCompleteLabel")}`
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

// Shared durable job status/actions for the import stream, history and detail.
const importJobUI = (() => {
  const terminalStatuses = new Set([
    "completed", "completed_with_errors", "failed", "interrupted",
  ]);

  function number(value) {
    return new Intl.NumberFormat().format(Number(value || 0));
  }

  function updateBadge(element, data) {
    if (!element) return;
    element.dataset.status = data.status;
    element.textContent = data.cancellation_pending
      ? element.dataset.cancelPendingLabel || data.status_display
      : data.status_display;
    element.classList.remove("success", "error", "warning", "info", "outline");
    if (data.status === "completed") element.classList.add("success");
    else if (["failed", "completed_with_errors"].includes(data.status)) {
      element.classList.add("error");
    } else if (["processing", "pending"].includes(data.status)) {
      element.classList.add("info");
    } else if (["paused", "interrupted"].includes(data.status)) {
      element.classList.add("warning");
    } else {
      element.classList.add("outline");
    }
  }

  function updateActionLinks(jobId, data) {
    document.querySelectorAll("[data-import-job-action-link]").forEach((link) => {
      if (link.dataset.jobId !== jobId) return;
      const action = link.dataset.importJobActionLink;
      const capability = {
        pause: data.can_pause,
        resume: data.can_resume,
        cancel: data.can_cancel,
      }[action];
      link.hidden = !capability;
    });
  }

  function active(data) {
    return ["pending", "processing", "paused"].includes(data.status)
      || Boolean(data.cancellation_pending);
  }

  function actionRoot(link) {
    return link.closest("[data-import-job-list-monitor]")
      || document.querySelector("[data-import-job-monitor]")
      || document.querySelector("[data-import-progress]");
  }

  async function postAction(root, action, url, feedback, button, onUpdate) {
    if (action === "cancel") {
      const message = root.dataset.cancelConfirmLabel;
      if (message && !window.confirm(message)) return;
    }
    if (button) button.disabled = true;
    if (feedback) feedback.textContent = root.dataset.actionPendingLabel || "";

    const csrfInput = root.querySelector('[data-import-job-csrf] input[name="csrfmiddlewaretoken"]')
      || root.closest("form")?.querySelector('input[name="csrfmiddlewaretoken"]')
      || document.querySelector('input[name="csrfmiddlewaretoken"]');
    const headers = {
      Accept: "application/json",
      "X-Requested-With": "XMLHttpRequest",
    };
    if (csrfInput) headers["X-CSRFToken"] = csrfInput.value;

    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers,
      });
      const result = await response.json();
      if (result.job && onUpdate) onUpdate(result.job);
      if (!response.ok || !result.ok) {
        throw new Error(result.message || root.dataset.actionErrorLabel || "Request failed.");
      }
      if (feedback) feedback.textContent = result.message || "";
    } catch (error) {
      if (feedback) {
        feedback.textContent = error.message || root.dataset.actionErrorLabel || "Request failed.";
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function readStatus(url) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Status unavailable.");
    return response.json();
  }

  return { number, updateBadge, updateActionLinks, active, actionRoot, postAction, readStatus };
})();

// The streaming importer reveals per-job controls as soon as the server has
// committed a durable ImportJob row. Buttons call the server and the UI polls
// the job record instead of treating browser-only progress as authoritative.
(function setupLiveImportJobControls() {
  const progressPanel = document.querySelector("[data-import-progress]");
  const jobList = progressPanel?.querySelector("[data-import-job-list]");
  const jobsPanel = progressPanel?.querySelector("[data-import-live-jobs]");
  if (!progressPanel || !jobList || !jobsPanel) return;

  const entries = new Map();
  const label = (key) => progressPanel.dataset[key] || "";

  function updateEntry(entry, data) {
    entry.data = data;
    const statusText = data.cancellation_pending
      ? label("cancelPendingLabel") : data.status_display;
    entry.status.textContent = `${statusText} · ${data.stage_display}`;
    entry.progress.value = data.progress_percent;
    entry.progress.setAttribute("aria-valuenow", String(Math.round(data.progress_percent)));
    entry.progress.setAttribute("aria-valuetext", `${data.progress_percent.toFixed(1)}%`);
    entry.percent.textContent = `${data.progress_percent.toFixed(1)}%`;
    entry.counts.textContent = `${label("committedLabel")}: ${importJobUI.number(data.total_committed)}; `
      + `${label("skippedLabel")}: ${importJobUI.number(data.total_skipped)}; `
      + `${label("failedCountLabel")}: ${importJobUI.number(data.total_failed)}; `
      + `${label("remainingLabel")}: ${importJobUI.number(data.remaining_count)}`;
    importJobUI.updateBadge(entry.badge, data);
    for (const [action, button] of Object.entries(entry.buttons)) {
      button.hidden = !data[`can_${action}`];
    }
    importJobUI.updateActionLinks(data.job_id, data);
  }

  function schedulePoll(entry, delay = 1800) {
    window.clearTimeout(entry.timer);
    if (!entry.data || !importJobUI.active(entry.data)) return;
    entry.timer = window.setTimeout(async () => {
      try {
        updateEntry(entry, await importJobUI.readStatus(entry.urls.status));
        schedulePoll(entry);
      } catch {
        entry.feedback.textContent = label("statusUnavailableLabel");
        schedulePoll(entry, 4000);
      }
    }, delay);
  }

  function addJob(event) {
    if (entries.has(event.job_id)) return;
    jobsPanel.hidden = false;

    const card = document.createElement("article");
    card.className = "import-live-job";
    const heading = document.createElement("div");
    heading.className = "import-live-job-heading";
    const jobLink = document.createElement("a");
    jobLink.className = "import-live-job-link";
    jobLink.href = event.detail_url;
    jobLink.textContent = `${label("importJobLabel")}: ${event.source_name || event.job_id}`;
    const badge = document.createElement("span");
    badge.className = "badge outline";
    badge.textContent = label("statusUnavailableLabel");
    badge.dataset.cancelPendingLabel = label("cancelPendingLabel");
    heading.append(jobLink, badge);

    const progressRow = document.createElement("div");
    progressRow.className = "import-live-job-progress-row";
    const progress = document.createElement("progress");
    progress.className = "import-live-job-progress";
    progress.max = 100;
    progress.value = 0;
    progress.setAttribute("aria-label", label("liveProgressLabel"));
    const percent = document.createElement("output");
    percent.textContent = "0%";
    progressRow.append(progress, percent);

    const status = document.createElement("p");
    status.className = "import-live-job-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const counts = document.createElement("p");
    counts.className = "field-help import-live-job-counts";
    const actions = document.createElement("div");
    actions.className = "import-live-job-actions";
    const feedback = document.createElement("p");
    feedback.className = "field-help import-live-job-feedback";
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");

    const urls = {
      pause: event.pause_url,
      resume: event.resume_url,
      cancel: event.cancel_url,
      status: event.status_url,
    };
    const buttons = {};
    for (const [action, key] of [
      ["pause", "pauseControlLabel"],
      ["resume", "continueControlLabel"],
      ["cancel", "stopControlLabel"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `button small-button${action === "cancel" ? " danger" : ""}`;
      button.textContent = label(key);
      button.hidden = true;
      button.addEventListener("click", () => {
        importJobUI.postAction(
          progressPanel, action, urls[action], feedback, button,
          (data) => updateEntry(entry, data),
        );
      });
      buttons[action] = button;
      actions.appendChild(button);
    }

    card.append(heading, status, progressRow, counts, actions, feedback);
    jobList.appendChild(card);
    const entry = { card, badge, status, progress, percent, counts, actions, feedback, buttons, urls, data: null, timer: null };
    entries.set(event.job_id, entry);
    updateEntry(entry, {
      job_id: event.job_id,
      status: "pending",
      status_display: label("statusUnavailableLabel"),
      stage_display: "",
      progress_percent: 0,
      total_committed: 0,
      total_skipped: 0,
      total_failed: 0,
      remaining_count: 0,
      can_pause: false,
      can_resume: false,
      can_cancel: true,
    });
    schedulePoll(entry, 0);
  }

  // Keep the event reader incremental: a job card appears before the first
  // (potentially large) durable import batch completes.
  progressPanel.addEventListener("import:job-created", (event) => addJob(event.detail));
  progressPanel._addLiveImportJob = addJob;
})();

(function setupImportJobDetailMonitor() {
  const root = document.querySelector("[data-import-job-monitor]");
  if (!root) return;
  const status = root.querySelector("[data-job-status]");
  const stage = root.querySelector("[data-job-stage]");
  const progress = root.querySelector("[data-job-progress]");
  const progressLabel = root.querySelector("[data-job-progress-label]");
  const feedback = root.querySelector("[data-job-feedback]");
  const counterNames = {
    discovered: "total_discovered",
    attempted: "total_attempted",
    committed: "total_committed",
    skipped: "total_skipped",
    failed: "total_failed",
    remaining: "remaining_count",
  };

  function update(data) {
    importJobUI.updateBadge(status, data);
    if (stage) stage.textContent = data.stage_display;
    if (progress) progress.style.width = `${Math.max(0, Math.min(100, data.progress_percent))}%`;
    if (progressLabel) progressLabel.textContent = `${data.progress_percent.toFixed(1)}%`;
    root.querySelectorAll("[data-job-counter]").forEach((element) => {
      const key = counterNames[element.dataset.jobCounter];
      if (key) element.textContent = importJobUI.number(data[key]);
    });
    // Update throughput, elapsed, and ETA if their elements exist
    const throughputEl = root.querySelector("[data-job-throughput]");
    if (throughputEl) {
      throughputEl.textContent = data.throughput_per_second > 0
        ? `${importJobUI.number(data.throughput_per_second)} lines/s`
        : "\u2014";
    }
    const elapsedEl = root.querySelector("[data-job-elapsed]");
    if (elapsedEl) {
      elapsedEl.textContent = data.elapsed_seconds > 0
        ? `${data.elapsed_seconds}s`
        : "\u2014";
    }
    const etaEl = root.querySelector("[data-job-eta]");
    if (etaEl) {
      if (data.eta_seconds > 0) {
        etaEl.textContent = `${data.eta_seconds}s`;
      } else if (data.eta_seconds === 0) {
        etaEl.textContent = "Done";
      } else {
        etaEl.textContent = "\u2014";
      }
    }
    importJobUI.updateActionLinks(data.job_id, data);
    if (feedback && data.cancellation_pending) {
      feedback.textContent = root.dataset.cancelPendingLabel || "";
    } else if (feedback && data.status === "paused") {
      feedback.textContent = data.status_display;
    }
  }

  let timer = null;
  async function poll() {
    try {
      const data = await importJobUI.readStatus(root.dataset.statusUrl);
      update(data);
      if (importJobUI.active(data)) {
        timer = window.setTimeout(poll, Number(root.dataset.pollInterval || 2000));
      }
    } catch {
      timer = window.setTimeout(poll, 5000);
    }
  }
  poll();
  window.addEventListener("pagehide", () => window.clearTimeout(timer), { once: true });
})();

(function setupImportJobListMonitor() {
  const root = document.querySelector("[data-import-job-list-monitor]");
  if (!root) return;
  const rows = [...root.querySelectorAll("[data-import-job-row]")];
  const counters = {
    attempted: "total_attempted",
    discovered: "total_discovered",
  };

  function updateRow(row, data) {
    importJobUI.updateBadge(row.querySelector("[data-job-status]"), data);
    const stage = row.querySelector("[data-job-stage]");
    if (stage) stage.textContent = data.stage_display;
    const progress = row.querySelector("[data-job-progress]");
    if (progress) progress.style.width = `${Math.max(0, Math.min(100, data.progress_percent))}%`;
    const progressLabel = row.querySelector("[data-job-progress-label]");
    if (progressLabel) progressLabel.textContent = `${data.progress_percent.toFixed(1)}%`;
    row.querySelectorAll("[data-job-counter]").forEach((element) => {
      const key = counters[element.dataset.jobCounter];
      if (key) element.textContent = importJobUI.number(data[key]);
    });
    const idLink = row.querySelector("td:first-child a");
    if (idLink) importJobUI.updateActionLinks(data.job_id, data);
    if (data.cancellation_pending) {
      const feedback = row.querySelector("[data-job-feedback]");
      if (feedback) feedback.textContent = root.dataset.cancelPendingLabel || "";
    }
  }

  rows.forEach((row) => {
    const status = row.querySelector("[data-job-status]")?.dataset.status;
    if (!["pending", "processing", "paused", "cancelled"].includes(status)) return;
    let timer = null;
    const poll = async () => {
      try {
        const data = await importJobUI.readStatus(row.dataset.statusUrl);
        updateRow(row, data);
        if (importJobUI.active(data)) {
          timer = window.setTimeout(poll, 2500);
        }
      } catch {
        timer = window.setTimeout(poll, 5000);
      }
    };
    poll();
    window.addEventListener("pagehide", () => window.clearTimeout(timer), { once: true });
  });
})();

(function setupImportJobActionLinks() {
  document.querySelectorAll("[data-import-job-action-link]").forEach((link) => {
    link.addEventListener("click", (event) => {
      const root = importJobUI.actionRoot(link);
      const url = link.dataset.postUrl;
      if (!root || !url) return; // No-JavaScript fallback: GET confirmation page.
      event.preventDefault();
      const row = link.closest("[data-import-job-row]");
      const feedback = row?.querySelector("[data-job-feedback]")
        || root.querySelector("[data-job-feedback]");
      importJobUI.postAction(
        root,
        link.dataset.importJobActionLink,
        url,
        feedback,
        link,
        (data) => {
          if (row) {
            row.querySelectorAll("[data-import-job-action-link]").forEach((sibling) => {
              const capability = {
                pause: data.can_pause,
                resume: data.can_resume,
                cancel: data.can_cancel,
              }[sibling.dataset.importJobActionLink];
              sibling.hidden = !capability;
            });
          } else {
            importJobUI.updateActionLinks(data.job_id, data);
          }
        },
      );
    });
  });
})();
