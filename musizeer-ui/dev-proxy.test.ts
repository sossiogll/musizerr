// @vitest-environment node
import http from "node:http";
import net from "node:net";
import os from "node:os";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createServer, type ViteDevServer } from "vite";
import { apiProxy } from "./dev-proxy";

/**
 * The dev proxy against a backend that misbehaves on the wire: a real Vite
 * dev server with the `/api` proxy of vite.config.ts in front of a raw
 * socket server, and a plain Node client. Every response must END — whole,
 * or as a failed transfer — within a few seconds, never stay open.
 */

const BODY = Buffer.alloc(4000, 7);

function head(extra: string): string {
  return `HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n${extra}\r\n`;
}

const answers: Record<string, (socket: net.Socket) => void> = {
  // Declares the whole body, sends a tenth and hangs up.
  "/cut": (socket) => {
    socket.write(head(`Content-Length: ${BODY.length}\r\n`));
    socket.write(BODY.subarray(0, 400));
    setTimeout(() => socket.destroy(), 50);
  },
  // Chunked, never terminated, then hangs up.
  "/cut-chunked": (socket) => {
    socket.write(head("Transfer-Encoding: chunked\r\n"));
    socket.write(`190\r\n${BODY.subarray(0, 400).toString("latin1")}\r\n`, "latin1");
    setTimeout(() => socket.destroy(), 50);
  },
  // Sends the headers and part of the body, then goes silent (socket open).
  "/stall": (socket) => {
    socket.write(head(`Content-Length: ${BODY.length}\r\n`));
    socket.write(BODY.subarray(0, 400));
  },
  "/whole": (socket) => {
    socket.end(Buffer.concat([Buffer.from(head(`Content-Length: ${BODY.length}\r\n`)), BODY]));
  },
};

let backend: net.Server;
let vite: ViteDevServer;
let base: string;
const sockets = new Set<net.Socket>();

beforeAll(async () => {
  backend = net.createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => {});
    let received = "";
    socket.on("data", (chunk) => {
      received += chunk.toString("latin1");
      if (!received.includes("\r\n\r\n")) return;
      const path = received.split(" ")[1];
      (answers[path] ?? ((s: net.Socket) => s.end("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")))(socket);
    });
  });
  await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
  const target = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;

  vite = await createServer({
    configFile: false,
    root: os.tmpdir(),
    logLevel: "silent",
    appType: "custom",
    optimizeDeps: { noDiscovery: true, include: [] },
    server: {
      host: "127.0.0.1",
      port: 0,
      hmr: false,
      ws: false,
      proxy: { "/api": apiProxy(target, { idleTimeoutMs: 1_000 }) },
    },
  });
  await vite.listen();
  base = `http://127.0.0.1:${(vite.httpServer!.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await vite?.close();
  for (const socket of sockets) socket.destroy();
  await new Promise((resolve) => backend?.close(resolve));
});

interface Outcome {
  status: number;
  bytes: number;
  complete: boolean;
  ms: number;
}

/** GET through the proxy; resolves when the response ends one way or the
 * other, rejects if it is still open after `limitMs`. */
function get(path: string, limitMs = 5_000): Promise<Outcome> {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const req = http.get(`${base}/api${path}`, { agent: false }, (res) => {
      let bytes = 0;
      let settled = false;
      const done = (complete: boolean) => {
        if (settled) return;
        settled = true;
        clearTimeout(limit);
        resolve({ status: res.statusCode ?? 0, bytes, complete, ms: Date.now() - started });
      };
      res.on("data", (chunk: Buffer) => (bytes += chunk.length));
      res.on("end", () => done(res.complete));
      res.on("aborted", () => done(false));
      res.on("error", () => done(false));
      res.on("close", () => done(res.complete));
    });
    const limit = setTimeout(() => {
      req.destroy();
      reject(new Error(`${path}: response still open after ${limitMs} ms`));
    }, limitMs);
    req.on("error", (err) => {
      clearTimeout(limit);
      reject(err);
    });
  });
}

describe("dev /api proxy", () => {
  it("passes a whole response through", async () => {
    const outcome = await get("/whole");
    expect(outcome).toMatchObject({ status: 200, bytes: BODY.length, complete: true });
  });

  it.each(["/cut", "/cut-chunked"])("ends the browser's response when the backend cuts %s mid-body", async (path) => {
    const outcome = await get(path);
    expect(outcome.status).toBe(200);
    expect(outcome.complete).toBe(false);
    expect(outcome.bytes).toBeLessThan(BODY.length);
    expect(outcome.ms).toBeLessThan(2_000);
  });

  it("cuts a backend that goes silent mid-body after the idle timeout", async () => {
    const outcome = await get("/stall");
    expect(outcome.complete).toBe(false);
    expect(outcome.ms).toBeLessThan(4_000);
  });

  it("a cut response does not hold the connection pool: later requests still pass", async () => {
    await Promise.all(Array.from({ length: 8 }, () => get("/cut")));
    const outcome = await get("/whole");
    expect(outcome.complete).toBe(true);
  });
});
