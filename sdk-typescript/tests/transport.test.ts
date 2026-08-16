import { describe, expect, it, vi } from "vitest";
import { Transport } from "../src/transport";
import { NotFoundError, ServerError, ConnectionError } from "../src/errors";

function mockFetch(status: number, body: string | object, headers?: Record<string, string>) {
  return vi.fn().mockResolvedValue({
    status,
    statusText: status >= 400 ? "Error" : "OK",
    headers: new Headers(headers),
    json: async () => (typeof body === "string" ? JSON.parse(body) : body),
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  });
}

describe("Transport", () => {
  it("makes GET requests with query params", async () => {
    const fetch = mockFetch(200, { data: "ok" });
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({ apiUrl: "http://localhost:8080" });
    const result = await transport.request("GET", "/api/v1/test", {
      params: { foo: "bar" },
    });

    expect(result).toEqual({ data: "ok" });
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8080/api/v1/test?foo=bar",
      expect.objectContaining({ method: "GET" }),
    );

    vi.unstubAllGlobals();
  });

  it("makes POST requests with JSON body", async () => {
    const fetch = mockFetch(200, { id: "123" });
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({ apiUrl: "http://localhost:8080" });
    await transport.request("POST", "/api/v1/test", {
      json: { name: "test" },
    });

    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8080/api/v1/test",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "test" }),
      }),
    );

    vi.unstubAllGlobals();
  });

  it("handles plain text errors from v2 API", async () => {
    const fetch = vi.fn().mockResolvedValue({
      status: 404,
      statusText: "Not Found",
      text: async () => "table not found",
    });
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({
      apiUrl: "http://localhost:8080",
      maxRetries: 1,
    });

    await expect(transport.request("GET", "/api/v1/test")).rejects.toThrow(
      NotFoundError,
    );
    await expect(transport.request("GET", "/api/v1/test")).rejects.toThrow(
      "table not found",
    );

    vi.unstubAllGlobals();
  });

  it("handles 500 server errors", async () => {
    const fetch = vi.fn().mockResolvedValue({
      status: 500,
      statusText: "Internal Server Error",
      text: async () => "database connection failed",
    });
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({
      apiUrl: "http://localhost:8080",
      maxRetries: 1,
    });

    await expect(transport.request("GET", "/api/v1/test")).rejects.toThrow(
      ServerError,
    );

    vi.unstubAllGlobals();
  });

  it("handles 204 No Content", async () => {
    const fetch = mockFetch(204, "");
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({ apiUrl: "http://localhost:8080" });
    const result = await transport.request("DELETE", "/api/v1/test");

    expect(result).toBeNull();

    vi.unstubAllGlobals();
  });

  it("retries on connection failure", async () => {
    let calls = 0;
    const fetch = vi.fn().mockImplementation(async () => {
      calls++;
      if (calls < 3) throw new Error("network error");
      return { status: 200, json: async () => ({ ok: true }), text: async () => '{"ok":true}' };
    });
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({
      apiUrl: "http://localhost:8080",
      maxRetries: 3,
    });

    const result = await transport.request("GET", "/api/v1/test");
    expect(result).toEqual({ ok: true });
    expect(calls).toBe(3);

    vi.unstubAllGlobals();
  });

  it("extracts message from structured error envelope", async () => {
    // ratd returns {"error": {"code": "INTERNAL", "type": "INTERNAL", "message": "failed to list grants"}}
    const fetch = mockFetch(
      500,
      { error: { code: "INTERNAL", type: "INTERNAL", message: "failed to list grants" } },
      { "content-type": "application/json" },
    );
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({ apiUrl: "http://localhost:8080", maxRetries: 1 });

    await expect(transport.request("GET", "/api/v1/permissions/grants")).rejects.toThrow(
      "failed to list grants",
    );
    await expect(transport.request("GET", "/api/v1/permissions/grants")).rejects.toThrow(
      ServerError,
    );

    vi.unstubAllGlobals();
  });

  it("handles plain string error field in JSON", async () => {
    const fetch = mockFetch(
      401,
      { error: "missing or invalid Authorization header" },
      { "content-type": "application/json" },
    );
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({ apiUrl: "http://localhost:8080", maxRetries: 1 });

    await expect(transport.request("GET", "/api/v1/test")).rejects.toThrow(
      "missing or invalid Authorization header",
    );

    vi.unstubAllGlobals();
  });

  it("throws ConnectionError after all retries exhausted", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("network error"));
    vi.stubGlobal("fetch", fetch);

    const transport = new Transport({
      apiUrl: "http://localhost:8080",
      maxRetries: 2,
    });

    await expect(transport.request("GET", "/api/v1/test")).rejects.toThrow(
      ConnectionError,
    );

    vi.unstubAllGlobals();
  });
});
