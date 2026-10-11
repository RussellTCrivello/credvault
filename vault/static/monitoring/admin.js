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
    if (emailField) {
      emailField.hidden = !isEmail;
      emailField.style.display = isEmail ? "" : "none";
    }
    if (userField) {
      userField.hidden = isEmail;
      userField.style.display = isEmail ? "none" : "";
    }
  }

  typeSelect.addEventListener("change", applyToggle);
  applyToggle();
})();

// Import form: a folder picker sends every file it contains, which is easy to
// underestimate. Show what was actually selected before submitting.
(function setupFolderPickerFeedback() {
  const folder = document.querySelector("#id_folder");
  if (!folder) return;
  const note = document.querySelector("[data-folder-count]");

  function describe() {
    const files = Array.from(folder.files || []);
    if (!note) return;
    if (!files.length) {
      note.hidden = true;
      return;
    }
    // Labels come from the template so they stay translated.
    const labels = note.dataset;
    const bytes = files.reduce((total, file) => total + file.size, 0);
    const mb = bytes / 1024 / 1024;
    const size = mb >= 1
      ? `${mb.toFixed(1)} MB`
      : `${Math.round(bytes / 1024)} KB`;
    const parts = [
      `${files.length} ${labels.filesLabel || "file(s) selected"}`,
      size,
    ];
    if (labels.hint) parts.push(labels.hint);
    note.hidden = false;
    note.textContent = parts.join(" · ");
  }

  folder.addEventListener("change", describe);
  describe();
})();
