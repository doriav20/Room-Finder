// Buttons that open and close a collapsed part of the page on a phone (the menu, the filters).
// Bootstrap's own script isn't loaded, so this does what its collapse plugin would.
document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-toggle]");
  if (!toggle) return;
  const target = document.querySelector(toggle.dataset.toggle);
  const open = target.classList.toggle("show");
  toggle.setAttribute("aria-expanded", String(open));
});

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
  const grid = document.getElementById("grid");
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
