import { defineConfig, type Plugin } from "vite";

// In `npm run dev`, answer /api/spotify-token with the same code the Cloudflare Pages
// Function runs (it is plain fetch + WebCrypto), so no wrangler is needed to develop.
function spotifyTokenDev(): Plugin {
  return {
    name: "spotify-token-dev",
    configureServer(server) {
      server.middlewares.use("/api/spotify-token", async (_req, res) => {
        const { onRequestGet } = await server.ssrLoadModule("/functions/api/spotify-token.ts");
        const answer: Response = await onRequestGet({} as never);
        res.statusCode = answer.status;
        answer.headers.forEach((value, key) => res.setHeader(key, value));
        res.end(await answer.text());
      });
    },
  };
}

export default defineConfig({
  plugins: [spotifyTokenDev()],
});
