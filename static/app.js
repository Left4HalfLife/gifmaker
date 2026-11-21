const form = document.querySelector("form");
const video = document.getElementById("video");
const dropZone = document.getElementById("drop-zone");
const pts = document.getElementById("pts");
const button = form.querySelector("button");
const status = document.getElementById("status");
const paletteSection = document.getElementById("palette-section");
const palettePreview = document.getElementById("palette-preview");
const paletteFilename = document.getElementById("palette-filename");
const gifSection = document.getElementById("gif-section");
const gifPreview = document.getElementById("gif-preview");
const gifDownload = document.getElementById("gif-download");
const csrfToken = form.elements.csrf_token.value;
let generatedGifUrl;
let cleanupSent = false;

const showFilename = () => {
  document.getElementById("filename").textContent = video.files[0]?.name || "";
};

const refreshPalette = async () => {
  const response = await fetch(form.dataset.paletteInfoUrl, { cache: "no-store" });
  if (!response.ok) {
    throw new Error("Could not load the saved palette.");
  }
  const palette = await response.json();
  if (!palette.available) {
    paletteSection.hidden = true;
    palettePreview.removeAttribute("src");
    return;
  }
  palettePreview.src = `${palette.url}?v=${Date.now()}`;
  paletteFilename.textContent = palette.filename;
  paletteSection.hidden = false;
};

video.addEventListener("change", showFilename);
pts.addEventListener("input", () => {
  document.getElementById("pts-value").textContent =
    `${pts.value} (${(1 / Number(pts.value)).toFixed(2)}× speed)`;
});

["dragenter", "dragover"].forEach(event => dropZone.addEventListener(event, e => {
  e.preventDefault();
  dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach(event => dropZone.addEventListener(event, e => {
  e.preventDefault();
  dropZone.classList.remove("dragging");
}));
dropZone.addEventListener("drop", e => {
  if (
    e.dataTransfer.files.length !== 1 ||
    !e.dataTransfer.files[0].name.toLowerCase().endsWith(".mp4")
  ) {
    status.textContent = "Please drop one MP4 file.";
    return;
  }
  video.files = e.dataTransfer.files;
  showFilename();
  status.textContent = "";
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  button.disabled = true;
  status.textContent = "Converting… please wait.";
  try {
    const response = await fetch(event.target.action, {
      method: "POST",
      body: new FormData(event.target),
    });
    if (!response.ok) {
      const html = new DOMParser().parseFromString(await response.text(), "text/html");
      throw new Error(
        html.querySelector('[role="alert"]')?.textContent || "Conversion failed."
      );
    }

    if (generatedGifUrl) {
      URL.revokeObjectURL(generatedGifUrl);
    }
    generatedGifUrl = URL.createObjectURL(await response.blob());
    gifPreview.src = generatedGifUrl;
    gifDownload.href = generatedGifUrl;
    gifDownload.download =
      video.files[0].name.replace(/\.mp4$/i, "") + ".gif";
    gifSection.hidden = false;
    status.textContent = "Done! Preview your GIF, then download it when ready.";
    await refreshPalette();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

window.addEventListener("pagehide", event => {
  if (cleanupSent || event.persisted) {
    return;
  }
  cleanupSent = true;
  const body = new FormData();
  body.append("csrf_token", csrfToken);
  if (!navigator.sendBeacon(form.dataset.cleanupUrl, body)) {
    fetch(form.dataset.cleanupUrl, {
      method: "POST",
      body,
      keepalive: true,
    });
  }
  if (generatedGifUrl) {
    URL.revokeObjectURL(generatedGifUrl);
  }
});

refreshPalette().catch(() => {
  paletteSection.hidden = true;
});
