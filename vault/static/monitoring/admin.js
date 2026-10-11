const themeToggle = document.querySelector("#theme-toggle");
const themeStorageKey = "logintel-theme";

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (themeToggle) {
    const dark = theme === "dark";
    themeToggle.textContent = dark
      ? themeToggle.dataset.lightLabel
      : themeToggle.dataset.darkLabel;
    themeToggle.setAttribute("aria-pressed", String(dark));
  }
}

try {
  setTheme(localStorage.getItem(themeStorageKey) === "dark" ? "dark" : "light");
} catch {
  setTheme("light");
}

themeToggle?.addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  setTheme(theme);
  try {
    localStorage.setItem(themeStorageKey, theme);
  } catch {
    // Theme switching remains available when storage is disabled.
  }
});

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
