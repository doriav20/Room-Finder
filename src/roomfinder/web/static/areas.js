// The neighborhoods map: click a neighborhood to choose it, or draw around several.
// The checkboxes next to the map hold the choice, and they are what the form saves.
(() => {
  const dataElement = document.getElementById("areas-data");
  if (!dataElement || !window.L) return;
  const data = JSON.parse(dataElement.textContent);
  const form = document.getElementById("areas-form");
  const boxes = new Map(
    Array.from(form.querySelectorAll('input[name="areas"]'), (box) => [box.value, box]),
  );
  const selectedBadge = document.getElementById("areas-selected");
  const drawButton = document.getElementById("areas-draw");
  let drawing = false;
  let lastDrawingEnded = 0;

  const map = L.map("areas-map", { zoomSnap: 0.5 });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // The dashboard sends no referrer to other sites, but OpenStreetMap's tile servers need one
    referrerPolicy: "strict-origin-when-cross-origin",
    attribution:
      '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · גבולות השכונות: עיריית ראשון לציון',
  }).addTo(map);

  const style = (chosen, hovered = false) => ({
    color: chosen ? "#0d6efd" : "#6c757d",
    weight: hovered ? 3 : chosen ? 2 : 1,
    fillColor: chosen ? "#0d6efd" : "#adb5bd",
    fillOpacity: chosen ? 0.35 : 0.08,
  });

  const textElement = (text) => {
    const span = document.createElement("span");
    span.textContent = text;
    return span;
  };

  const polygons = new Map();
  const labels = L.layerGroup();
  for (const area of data.areas) {
    const box = boxes.get(area.name);
    const polygon = L.polygon(area.outline, style(box.checked)).addTo(map);
    polygon.bindTooltip(textElement(`${area.name} · ${area.count} מודעות`), {
      sticky: true,
      direction: "top",
    });
    polygon.on("mouseover", () => polygon.setStyle(style(box.checked, true)));
    polygon.on("mouseout", () => polygon.setStyle(style(box.checked)));
    polygon.on("click", () => {
      // The click that ends a drawing isn't meant for the neighborhood under it
      if (drawing || Date.now() - lastDrawingEnded < 400) return;
      box.checked = !box.checked;
      refresh();
    });
    polygons.set(area.name, polygon);
    L.tooltip({ permanent: true, direction: "center", className: "area-label", interactive: false })
      .setLatLng(area.label)
      .setContent(textElement(area.name))
      .addTo(labels);
  }
  map.fitBounds(data.bounds);

  // Names on the map only once they fit
  const showLabels = () => (map.getZoom() >= 14 ? labels.addTo(map) : labels.remove());
  map.on("zoomend", showLabels);
  showLabels();

  function refresh() {
    let chosen = 0;
    for (const [name, polygon] of polygons) {
      const checked = boxes.get(name).checked;
      chosen += checked ? 1 : 0;
      polygon.setStyle(style(checked));
    }
    selectedBadge.textContent = chosen ? `${chosen} נבחרו` : "כל העיר";
  }
  form.addEventListener("change", refresh);
  for (const button of form.querySelectorAll("[data-choose]")) {
    button.addEventListener("click", () => {
      const checked = button.dataset.choose === "all";
      for (const box of boxes.values()) box.checked = checked;
      refresh();
    });
  }
  refresh();

  // Drawing: press and drag around neighborhoods; every one whose middle is inside gets chosen
  const container = map.getContainer();
  let points = [];
  let line = null;

  function setDrawing(on) {
    drawing = on;
    drawButton.classList.toggle("active", on);
    container.classList.toggle("areas-drawing", on);
    if (on) map.dragging.disable();
    else map.dragging.enable();
  }
  drawButton.addEventListener("click", () => setDrawing(!drawing));

  container.addEventListener("pointerdown", (event) => {
    if (!drawing || event.button !== 0) return;
    event.preventDefault();
    container.setPointerCapture(event.pointerId);
    points = [map.mouseEventToLatLng(event)];
    line = L.polyline(points, { color: "#fd7e14", weight: 2, dashArray: "6 4", interactive: false })
      .addTo(map);
  });
  container.addEventListener("pointermove", (event) => {
    if (!line) return;
    points.push(map.mouseEventToLatLng(event));
    line.setLatLngs(points);
  });
  const finish = () => {
    if (!line) return;
    line.remove();
    line = null;
    if (points.length > 2) {
      for (const area of data.areas) {
        if (inside(area.label, points)) boxes.get(area.name).checked = true;
      }
      refresh();
    }
    points = [];
    lastDrawingEnded = Date.now();
    setDrawing(false);
  };
  container.addEventListener("pointerup", finish);
  container.addEventListener("pointercancel", finish);

  function inside([lat, lng], outline) {
    let hit = false;
    for (let i = 0, j = outline.length - 1; i < outline.length; j = i++) {
      const a = outline[i];
      const b = outline[j];
      if (a.lat > lat !== b.lat > lat && lng < ((b.lng - a.lng) * (lat - a.lat)) / (b.lat - a.lat) + a.lng) {
        hit = !hit;
      }
    }
    return hit;
  }
})();
