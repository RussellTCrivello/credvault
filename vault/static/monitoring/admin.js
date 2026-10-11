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
