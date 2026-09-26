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
