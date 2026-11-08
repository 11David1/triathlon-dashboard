const $ = (id) => document.getElementById(id);
let setup = false;
let minLength = 12;

function showError(text) {
  $("error").textContent = text;
  $("error").hidden = false;
}

function enterSetup(min) {
  setup = true;
  minLength = min || minLength;
  $("step-login").hidden = true;
  $("step-setup").hidden = false;
  $("new-password").required = true;
  $("new-password-2").required = true;
  $("new-password").minLength = minLength;
  $("new-password").placeholder = `mindestens ${minLength} Zeichen`;
  $("submit").textContent = "Passwort speichern und anmelden";
  $("new-password").focus();
}

$("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("error").hidden = true;
  const body = { username: $("username").value.trim(), password: $("password").value };
  if (setup) {
    if ($("new-password").value.length < minLength) return showError(`Das Passwort braucht mindestens ${minLength} Zeichen.`);
    if ($("new-password").value !== $("new-password-2").value) return showError("Die beiden Passwörter stimmen nicht überein.");
    body.new_password = $("new-password").value;
  }
  $("submit").disabled = true;
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => null);
  $("submit").disabled = false;
  if (response && response.ok) {
    location.href = "/";
    return;
  }
  const data = response ? await response.json().catch(() => ({})) : {};
  if (data.setup && !setup) {
    enterSetup(data.min_length);
    return;
  }
  if (response && response.status === 401 && setup) {
    // Startcode inzwischen abgelaufen oder ersetzt: zurück zum ersten Schritt.
    setup = false;
    $("step-login").hidden = false;
    $("step-setup").hidden = true;
    $("new-password").required = $("new-password-2").required = false;
    $("submit").textContent = "Anmelden";
  }
  showError(data.detail || "Server nicht erreichbar.");
});
