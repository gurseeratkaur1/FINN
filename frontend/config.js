// API base URL. Served by FastAPI (same origin) -> "". Opened from another static server -> local backend.
// For a Vercel deploy, set this to the deployed backend URL (e.g. "https://finn-api.example.com").
window.FINN_API_URL = window.FINN_API_URL ?? (location.port === "8000" ? "" : "http://localhost:8000");
