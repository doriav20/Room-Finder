// Buttons that open and close a collapsed part of the page on a phone (the menu, the filters).
// Bootstrap's own script isn't loaded, so this does what its collapse plugin would.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-toggle]");
  if (!toggle) return;
  const target = document.querySelector(toggle.dataset.toggle);
  const open = target.classList.toggle("show");
  toggle.setAttribute("aria-expanded", String(open));
});

// Light or dark: the navbar's button goes from following the device, to light, to dark and back.
// The page's <head> already applied the theme before it was drawn (partials/theme_head.html).
(function () {
  const button = document.getElementById("theme-toggle");
  const deviceDark = matchMedia("(prefers-color-scheme: dark)");
  const choices = {
    auto: ["🌓", "ערכת צבעים: לפי המכשיר"],
    light: ["☀️", "ערכת צבעים: בהירה"],
    dark: ["🌙", "ערכת צבעים: כהה"],
  };
  let choice = "auto";
  try {
    const saved = localStorage.getItem("theme");
    if (saved in choices) choice = saved;
  } catch (error) {
    // Without storage the choice lasts until the next page
  }

  const apply = () => {
    const theme = choice === "auto" ? (deviceDark.matches ? "dark" : "light") : choice;
    document.documentElement.dataset.bsTheme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#212529" : "#ffffff");
    if (!button) return;
    const [icon, label] = choices[choice];
    button.textContent = icon;
    button.title = label;
    button.setAttribute("aria-label", label);
  };

  button?.addEventListener("click", () => {
    choice = { auto: "light", light: "dark", dark: "auto" }[choice];
    try {
      if (choice === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", choice);
    } catch (error) {
      // As above
    }
    apply();
  });
  deviceDark.addEventListener("change", apply);
  apply();
})();

// A dropdown of checkboxes (the sites filter): picking several sites takes several clicks,
// so the filter is applied once, when the list closes, rather than on every click
document.querySelectorAll("details.multi-select").forEach((dropdown) => {
  const checked = () => [...dropdown.querySelectorAll("input:checked")].map((input) => input.value).join();
  let opened = checked();
  dropdown.addEventListener("toggle", () => {
    if (dropdown.open) opened = checked();
    else if (checked() !== opened) dropdown.closest("form").submit();
  });
});

// A click outside an open dropdown closes it
document.addEventListener("click", (event) => {
  document.querySelectorAll("details.multi-select[open]").forEach((dropdown) => {
    if (!dropdown.contains(event.target)) dropdown.open = false;
  });
});

// On a phone the listing tabs scroll sideways; bring the chosen one into view
document.querySelector(".listing-tabs .active")?.scrollIntoView({ block: "nearest", inline: "nearest" });

// Polls for new listings while the dashboard is open, and announces them with a
// banner, a browser notification and a short sound.
(function () {
  const POLL_MS = 60 * 1000;
  // The cards grid, or the map in its place
  const grid = document.querySelector("[data-latest-id]");
  const button = document.getElementById("enable-notifications");
  const baseTitle = document.title;

  if (button && "Notification" in window && Notification.permission === "default") {
    button.hidden = false;
    button.addEventListener("click", async () => {
      await Notification.requestPermission();
      button.hidden = true;
    });
  }

  if (!grid) return;
  let sinceId = Number(grid.dataset.latestId || 0);
  let unseen = 0;

  function beep() {
    try {
      const context = new AudioContext();
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.15, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.4);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillator.stop(context.currentTime + 0.4);
    } catch (error) {
      // Sound is a nicety; browsers may block it until the user interacts with the page
    }
  }

  async function poll() {
    try {
      const response = await fetch(`/api/updates?since_id=${sinceId}`);
      if (!response.ok) return;
      const data = await response.json();
      sinceId = Math.max(sinceId, data.latest_id);
      if (!data.listings.length) return;

      unseen += data.listings.length;
      document.getElementById("new-count").textContent = unseen;
      document.getElementById("new-banner").hidden = false;
      document.title = `(${unseen}) ${baseTitle}`;
      beep();
      if ("Notification" in window && Notification.permission === "granted") {
        for (const listing of data.listings.slice(0, 3)) {
          const notification = new Notification("🏠 דירה חדשה", { body: listing.summary, lang: "he", dir: "rtl" });
          notification.onclick = () => window.open(listing.url, "_blank", "noopener");
        }
      }
    } catch (error) {
      // The app may be restarting; try again on the next round
    }
  }

  setInterval(poll, POLL_MS);
})();

// The photos on a listing card. Cards also arrive later (htmx), so this listens on the page.
(function () {
  const galleryOf = (element) => element.closest(".position-relative")?.querySelector(".listing-gallery");

  const showIndex = (gallery) => {
    const counter = gallery.parentElement.querySelector("[data-gallery-index]");
    if (!counter || !gallery.clientWidth) return;
    // In a right-to-left page scrollLeft runs from 0 down to negative numbers
    counter.textContent = Math.round(Math.abs(gallery.scrollLeft) / gallery.clientWidth) + 1;
  };

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".gallery-nav");
    const gallery = button && galleryOf(button);
    if (!gallery) return;
    const step = button.classList.contains("gallery-next") ? 1 : -1;
    const rtl = getComputedStyle(gallery).direction === "rtl";
    gallery.scrollBy({ left: step * (rtl ? -1 : 1) * gallery.clientWidth, behavior: "smooth" });
  });

  // Scrolling doesn't bubble, so it's caught on its way down
  document.addEventListener(
    "scroll",
    (event) => {
      if (event.target instanceof Element && event.target.matches(".listing-gallery")) showIndex(event.target);
    },
    true,
  );

  // A photo the site no longer serves is dropped from the strip
  document.addEventListener(
    "error",
    (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement) || !image.parentElement?.matches(".listing-gallery")) return;
      const gallery = image.parentElement;
      const frame = gallery.parentElement;
      image.remove();
      const left = gallery.querySelectorAll("img").length;
      const total = frame.querySelector("[data-gallery-total]");
      if (total) total.textContent = left;
      if (left < 2) frame.querySelectorAll(".gallery-nav, .gallery-count").forEach((node) => node.remove());
      if (!left) {
        gallery.className = "card-img-top listing-photo listing-photo-empty";
        gallery.textContent = "🏠";
      }
      showIndex(gallery);
    },
    true,
  );
})();

// A card's notes save when the field is left; a green or red frame says whether they did
document.addEventListener("htmx:afterRequest", (event) => {
  const field = event.detail.elt;
  if (!(field instanceof HTMLTextAreaElement) || !field.matches(".notes")) return;
  field.classList.remove("is-valid", "is-invalid");
  field.classList.add(event.detail.successful ? "is-valid" : "is-invalid");
  if (event.detail.successful) setTimeout(() => field.classList.remove("is-valid"), 2000);
});

// A button back to the top, once a long list has been scrolled well down
(function () {
  const button = document.getElementById("back-to-top");
  if (!button) return;
  let ticking = false;
  window.addEventListener(
    "scroll",
    () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        button.hidden = window.scrollY < window.innerHeight * 1.5;
        ticking = false;
      });
    },
    { passive: true },
  );
  button.addEventListener("click", () => {
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
  });
})();
