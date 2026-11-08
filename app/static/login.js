document.getElementById("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("error");
  error.hidden = true;
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: document.getElementById("password").value }),
  }).catch(() => null);
  if (response && response.ok) {
    location.href = "/";
    return;
  }
  const data = response ? await response.json().catch(() => ({})) : {};
  error.textContent = data.detail || "Server nicht erreichbar.";
  error.hidden = false;
});
