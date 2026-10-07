"use strict";

const TOKEN_HASH = /^#([A-Za-z0-9_-]+)$/;

// The token arrives in the URL fragment (never sent to the server). Keep it in
// sessionStorage (falling back to memory if storage is blocked) and rewrite this
// tab's history entry without it; the original URL may remain in browser history.
function takeToken() {
  const match = TOKEN_HASH.exec(location.hash);
  let token = match ? match[1] : null;
  try {
    if (token) {
      sessionStorage.setItem("token", token);
    } else {
      token = sessionStorage.getItem("token");
    }
  } catch (error) {
    // storage blocked: the token lives for this page load only
  }
  if (match) {
    history.replaceState(null, "", location.pathname);
  }
  return token;
}

const token = takeToken();

function api(path, options = {}) {
  const headers = { ...options.headers, Authorization: "Bearer " + token };
  return fetch(path, { ...options, headers });
}

function show(text) {
  document.getElementById("status").textContent = text;
}

async function main() {
  if (!token) {
    show("No token. Open the URL printed by `image-review serve --socket`.");
    return;
  }
  try {
    const response = await api("/version");
    if (response.status === 401) {
      show("token rejected (server restarted?)");
    } else if (response.ok) {
      const info = await response.json();
      show("connected (server " + info.version + ")");
    } else {
      show("connection error (HTTP " + response.status + ")");
    }
  } catch (error) {
    show("connection error");
  }
}

main();
