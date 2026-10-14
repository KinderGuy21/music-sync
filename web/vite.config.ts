import { defineConfig, type Plugin } from "vite";

// In `npm run dev`, answer /api/spotify-token and /api/youtube-search with the same code the
// Cloudflare Pages Functions run (plain fetch + WebCrypto), so no wrangler is needed to develop.
function pagesFunctionsDev(): Plugin {
  return {
    name: "pages-functions-dev",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = (req as { url?: string }).url ?? "";
        const name = /^\/api\/(spotify-token|youtube-search)(?:\?|$)/.exec(url)?.[1];
        if (!name) return next();
        const { onRequestGet } = await server.ssrLoadModule(`/functions/api/${name}.ts`);
        const request = new Request(new URL(url, "http://localhost"));
        const answer: Response = await onRequestGet({ request } as never);
        res.statusCode = answer.status;
        answer.headers.forEach((value, key) => res.setHeader(key, value));
        res.end(await answer.text());
      });
    },
  };
}

export default defineConfig({
  plugins: [pagesFunctionsDev()],
});
