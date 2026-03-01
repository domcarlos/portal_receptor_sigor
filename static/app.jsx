const { useState, useEffect, useCallback } = React;

const API_BASE = "https://portal-receptor-sigor.onrender.com/api";

async function api(path, opts = {}) {
  const res = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json", Accept: "application/json", ...opts.headers },
    ...opts,
  });
  if (opts.method === "DELETE" && res.status === 204) return null;
  return res.json();
}
