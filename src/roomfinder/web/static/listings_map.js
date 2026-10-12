// The map view of the listings page: each listing of the tab as its price on the map.
// The listings come from /api/listings/map with the page's own tab and filters.
(() => {
  const element = document.getElementById("listings-map");
  if (!element || !window.L) return;
  const note = document.getElementById("listings-map-note");

  const map = L.map(element, { zoomSnap: 0.5 }).setView([31.965, 34.8], 13);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // The dashboard sends no referrer to other sites, but OpenStreetMap's tile servers need one
    referrerPolicy: "strict-origin-when-cross-origin",
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  const shortPrice = (price) => {
    if (!price) return "?";
    return price >= 1000 ? `${(price / 1000).toFixed(1).replace(/\.0$/, "")}K` : String(price);
  };

  const pin = (listing) => {
    const label = make("span", `price-pin status-${listing.status}`, shortPrice(listing.price));
    if (listing.approximate) label.classList.add("approximate");
    return L.divIcon({ className: "price-pin-anchor", html: label, iconSize: [0, 0] });
  };

  const popup = (listing) => {
    const box = make("div", "map-popup");
    box.dir = "rtl";
    if (listing.image) {
      const image = make("img", "map-popup-photo");
      image.src = listing.image;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => image.remove());
      box.append(image);
    }
    const price = listing.price ? `${listing.price.toLocaleString("en-US")} ₪` : "מחיר לא צוין";
    box.append(make("div", "fw-bold fs-6", price));
    const specs = [listing.rooms && `${listing.rooms} חדרים`, listing.sqm && `${listing.sqm} מ״ר`];
    box.append(make("div", "", specs.filter(Boolean).join(" · ")));
    // The neighborhood by the city's map, unless the address already names it
    const area = listing.area && !(listing.address || "").includes(listing.area) && `(${listing.area})`;
    const place = [listing.address, area];
    box.append(make("div", "text-body-secondary", `📍 ${place.filter(Boolean).join(" ") || "כתובת לא צוינה"}`));
    if (listing.approximate) {
      box.append(make("div", "small text-warning-emphasis", "מיקום משוער, באמצע השכונה שבמודעה"));
    }
    const link = make("a", "btn btn-sm btn-primary",`למודעה ב${listing.source} ↗`);
    link.href = listing.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const actions = make("div", "d-flex align-items-center justify-content-between gap-2 mt-2");
    actions.append(link);
    if (listing.directions) {
      // Google Maps opens with the apartment as the start, on the way to the user's destination
      const directions = make("a", "directions-link", "מסלול");
      directions.href = listing.directions;
      directions.target = "_blank";
      directions.rel = "noopener noreferrer";
      directions.title = "מסלול בגוגל מפות שיוצא מהדירה";
      actions.append(directions);
    }
    box.append(actions);
    return box;
  };

  // Listings at the very same point (one building, or the middle of a neighborhood) are
  // spread around it a little, so each can be clicked
  const spread = (listings) => {
    const taken = new Map();
    return listings.map((listing) => {
      const key = `${listing.lat.toFixed(5)},${listing.lng.toFixed(5)}`;
      const index = taken.get(key) || 0;
      taken.set(key, index + 1);
      if (!index) return [listing.lat, listing.lng];
      const angle = index * 2.39996; // the golden angle, so the spiral doesn't line up
      const radius = 0.0007 * Math.sqrt(index);
      return [listing.lat + radius * Math.sin(angle), listing.lng + radius * 1.2 * Math.cos(angle)];
    });
  };

  const say = (text) => {
    if (note) note.textContent = text;
  };

  fetch(element.dataset.src)
    .then((response) => {
      if (!response.ok) throw new Error(response.statusText);
      return response.json();
    })
    .then((data) => {
      for (const area of data.areas) {
        L.polygon(area.outline, {
          interactive: false,
          color: "#0d6efd",
          weight: 1.5,
          dashArray: "4 4",
          fillOpacity: 0.04,
        }).addTo(map);
      }
      const points = spread(data.listings);
      // Where pins overlap, the first in the chosen order (newest, or cheapest) is on top
      data.listings.forEach((listing, index) => {
        L.marker(points[index], { icon: pin(listing), zIndexOffset: -1000 * index, title: listing.address || "" })
          .bindPopup(popup(listing), { maxWidth: 260, minWidth: 200 })
          .addTo(map);
      });
      const fit = () => {
        if (points.length) map.fitBounds(points, { padding: [30, 30], maxZoom: 16 });
        else if (data.bounds) map.fitBounds(data.bounds);
      };
      fit();
      // A map laid out while the page was hidden has no size; fit it again once it shows
      let width = element.clientWidth;
      new ResizeObserver(() => {
        map.invalidateSize();
        if (!width && element.clientWidth) fit();
        width = element.clientWidth;
      }).observe(element);
      const parts = [];
      if (!data.listings.length) parts.push("אין מודעות להצגה על המפה.");
      parts.push("לחיצה על מחיר פותחת את פרטי המודעה. מסגרת מקווקוות: מיקום משוער, באמצע השכונה שבמודעה.");
      if (data.without_location === 1) {
        parts.push("מודעה אחת בלי מיקום ובלי שכונה לא מוצגת (היא ברשימה).");
      } else if (data.without_location) {
        parts.push(`${data.without_location} מודעות בלי מיקום ובלי שכונה לא מוצגות (הן ברשימה).`);
      }
      say(parts.join(" "));
    })
    .catch(() => say("טעינת המודעות למפה נכשלה. אפשר לרענן את הדף."));
})();
