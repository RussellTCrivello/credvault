// Account menu: theme choice, popover focus handling, and keeping the
// trigger's aria-expanded state in sync. The popover itself (open, close,
// Escape, click outside) is handled by the browser's Popover API.
(function setupProfileMenu() {
  const storageKey = "logintel-theme";
  const choices = [...document.querySelectorAll("[data-theme-choice]")];
  const trigger = document.querySelector("#profile-trigger");
  const popover = document.querySelector("#profile-popover");

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    choices.forEach((choice) => {
      choice.setAttribute(
        "aria-pressed",
        String(choice.dataset.themeChoice === theme),
      );
      choice.classList.toggle(
        "is-selected",
        choice.dataset.themeChoice === theme,
      );
    });
  }

  try {
    setTheme(localStorage.getItem(storageKey) === "dark" ? "dark" : "light");
  } catch {
    setTheme("light");
  }

  choices.forEach((choice) => {
    choice.addEventListener("click", () => {
      const theme = choice.dataset.themeChoice;
      setTheme(theme);
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // Theme switching remains available when storage is disabled.
      }
    });
  });

  if (!trigger || !popover) return;

  popover.addEventListener("toggle", (event) => {
    const open = event.newState === "open";
    trigger.setAttribute("aria-expanded", String(open));
    if (open) {
      // Move focus into the menu so keyboard users land on the first action.
      const first = popover.querySelector(
        "a, button:not([disabled]), input, select",
      );
      first?.focus({ preventScroll: true });
    }
  });
})();
