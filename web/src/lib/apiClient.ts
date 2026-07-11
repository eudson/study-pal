import { client } from "../api/client.gen";

// Configured once at startup (imported from main.tsx). Default to same-origin
// so browser requests go through the Vite dev proxy / reverse proxy and never
// need CORS.
client.setConfig({
  baseUrl: import.meta.env.VITE_API_BASE_URL || "/",
});
