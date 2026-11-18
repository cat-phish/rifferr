import http from "node:http";
import type { AddressInfo } from "node:net";
import { createServer } from "vite";
import { expect, test } from "vitest";

// SEC-48: in --dev the backend's Host check is bypassed by the proxy, so Vite itself must
// refuse a request whose Host is not a loopback name (DNS rebinding).
function get(port: number, host: string): Promise<number> {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port, path: "/api/profiles", headers: { host } }, (res) => {
        res.resume();
        resolve(res.statusCode ?? 0);
      })
      .on("error", reject);
  });
}

test("SEC-48 vite refuses a foreign Host", async () => {
  const server = await createServer({
    configFile: `${process.cwd()}/vite.config.ts`,
    server: { port: 0, strictPort: false },
    logLevel: "silent",
  });
  await server.listen();
  try {
    const port = (server.httpServer?.address() as AddressInfo).port;
    expect(await get(port, `example.test:${String(port)}`)).toBe(403);
    expect(await get(port, `127.0.0.1:${String(port)}`)).not.toBe(403);
  } finally {
    await server.close();
  }
});
