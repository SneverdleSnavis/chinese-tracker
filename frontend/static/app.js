const API = "";

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

function navHtml(active) {
  const items = [
    ["/", "Dashboard"],
    ["/read", "Reading"],
    ["/words", "Words"],
  ];
  return `<nav><span class="brand">中文 Tracker</span>${items
    .map(
      ([href, label]) =>
        `<a href="${href}" class="${active === href ? "active" : ""}">${label}</a>`
    )
    .join("")}</nav>`;
}
