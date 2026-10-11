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
