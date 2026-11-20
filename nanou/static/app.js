const $ = id => document.getElementById(id);
let busy = false;
let status = null;

function message(text = "", error = false) {
  $("feedback").textContent = text;
  $("feedback").classList.toggle("error", error);
}

function updateButtons() {
  const ids = ["send-code", "sign-in", "refresh", "disconnect", "sign-out"];
  for (const id of ids) $(id).disabled = busy;
  $("connect").disabled = busy || !$("device").value;
  $("device").disabled = busy;
  $("scheme").disabled = busy;
}

function applyStatus(next) {
  const previous = status;
  status = next;
  $("account-status").textContent = status.signed_in ? "UU signed in" : "UU signed out";
  $("account-status").classList.toggle("online", status.signed_in);
  $("sign-out").hidden = !status.signed_in;
  $("login").hidden = status.signed_in;
  $("devices").hidden = !status.signed_in || !!status.session;
  $("session").hidden = !status.session;
  if (status.session) {
    $("gateway-name").textContent = status.session.name;
    $("console").href = status.session.url;
  } else {
    $("console").removeAttribute("href");
  }
  if (previous?.session && !status.session && !busy) {
    message("The tunnel closed. Refresh the gateway list to reconnect.", true);
  }
}

async function request(name, args) {
  const options = args === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(args),
  };
  const response = await fetch("/api/" + name, options);
  const data = await response.json();
  if (typeof data.signed_in === "boolean") applyStatus(data);
  if (!response.ok) throw new Error(data.error || "The request failed.");
  return data.result;
}

async function act(progress, action) {
  if (busy) return;
  busy = true;
  updateButtons();
  message(progress);
  try {
    await action();
  } catch (error) {
    message(error.message || "NanoU could not complete the request.", true);
  } finally {
    busy = false;
    updateButtons();
  }
}

async function loadDevices() {
  const select = $("device");
  const selected = select.value;
  const devices = await request("devices", {});
  select.replaceChildren(new Option("Select a gateway", ""));
  const sorted = devices.sort((a, b) => Number(b.available) - Number(a.available)
    || a.name.localeCompare(b.name));
  for (const device of sorted) {
    const suffix = device.available ? "" : device.online ? " · Unavailable" : " · Offline";
    select.add(new Option(device.name + suffix, device.id, false, device.id === selected && device.available));
    select.lastElementChild.disabled = !device.available;
  }
  message(devices.some(device => device.available) ? "" : "No available gateway. Start it on the NanoKVM network and refresh.");
}

async function sendCode() {
  if (!$("phone").reportValidity()) return;
  await act("Requesting a verification code…", async () => {
    await request("sms", {phone: $("phone").value});
    $("code-fields").hidden = false;
    $("code").required = true;
    $("send-code").textContent = "Resend code";
    $("send-code").classList.add("quiet");
    $("sign-in").hidden = false;
    $("code").focus();
    message("Enter the verification code from your SMS.");
  });
}

$("send-code").addEventListener("click", sendCode);
$("login-form").addEventListener("submit", event => {
  event.preventDefault();
  if ($("code-fields").hidden) {
    void sendCode();
    return;
  }
  void act("Signing in…", async () => {
    await request("sign-in", {phone: $("phone").value, code: $("code").value});
    $("login-form").reset();
    $("code").required = false;
    $("code-fields").hidden = true;
    $("sign-in").hidden = true;
    $("send-code").textContent = "Send SMS code";
    $("send-code").classList.remove("quiet");
    message("Loading your gateways…");
    await loadDevices();
  });
});
$("refresh").addEventListener("click", () => act("Refreshing gateways…", loadDevices));
$("device").addEventListener("change", updateButtons);
$("connect-form").addEventListener("submit", event => {
  event.preventDefault();
  void act("Opening the UU tunnel…", async () => {
    await request("connect", {device_id: $("device").value, scheme: $("scheme").value});
    message();
    $("console").focus();
  });
});
$("disconnect").addEventListener("click", () => act("Disconnecting…", async () => {
  await request("disconnect", {});
  message("Disconnected.");
}));
$("sign-out").addEventListener("click", () => act("Signing out…", async () => {
  await request("sign-out", {});
  $("device").replaceChildren(new Option("Refresh to find gateways", ""));
  message("Signed out.");
}));

void act("Starting NanoU…", async () => {
  await request("status");
  message();
  if (status.signed_in && !status.session) await loadDevices();
});
setInterval(async () => {
  if (busy) return;
  try {
    await request("status");
  } catch {
    message("The local NanoU app is unavailable. Check the window where you started it.", true);
  }
}, 4000);
