const box = document.getElementById("enabled");
chrome.storage.sync.get({ enabled: true }, ({ enabled }) => { box.checked = enabled; });
box.addEventListener("change", () => chrome.storage.sync.set({ enabled: box.checked }));
document.getElementById("server").textContent = new URL(CS_API_BASE).host;
document.getElementById("site").href = CS_API_BASE;
