// API base URL. Served by FastAPI (same origin) -> "". Opened from another static server -> local backend.
// For a Vercel deploy, set this to the deployed backend URL (e.g. "https://finn-api.example.com").
// Same origin everywhere (Render, port 8000) except a local static dev server on another port.
const _isLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
window.FINN_API_URL = window.FINN_API_URL ?? (_isLocal && location.port && location.port !== "8000" ? "http://localhost:8000" : "");
