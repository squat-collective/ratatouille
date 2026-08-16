import type { ClientConfig } from "./config";
import { DEFAULT_CONFIG } from "./config";
import { ConnectionError, RatError, errorFromStatus } from "./errors";

/**
 * Any JSON-serializable object that can be sent as a request body.
 * Uses `object` so typed interfaces (e.g., CreatePipelineRequest) are
 * assignable without casts, while still excluding primitives.
 */
type JsonBody = object;

export class Transport {
  private config: ClientConfig;

  constructor(config: ClientConfig) {
    this.config = config;
  }

  async request<T = unknown>(
    method: string,
    path: string,
    options?: {
      json?: JsonBody;
      params?: Record<string, string>;
      body?: BodyInit;
      rawHeaders?: Record<string, string>;
      signal?: AbortSignal;
    },
  ): Promise<T> {
    const headers: Record<string, string> = {
      ...this.config.headers,
      ...options?.rawHeaders,
    };

    if (options?.json) {
      headers["Content-Type"] = "application/json";
    }

    let url = `${this.config.apiUrl}${path}`;
    if (options?.params) {
      const qs = new URLSearchParams(options.params).toString();
      url += `?${qs}`;
    }

    const timeout = this.config.timeout ?? DEFAULT_CONFIG.timeout;

    // Only retry idempotent methods to prevent duplicate side effects (e.g., duplicate pipeline runs)
    const IDEMPOTENT_METHODS = new Set(["GET", "PUT", "DELETE", "HEAD", "OPTIONS"]);
    const maxRetries = IDEMPOTENT_METHODS.has(method.toUpperCase())
      ? (this.config.maxRetries ?? DEFAULT_CONFIG.maxRetries)
      : 1;

    // Run request interceptors (e.g., add auth tokens, trace IDs)
    if (this.config.onRequest) {
      for (const interceptor of this.config.onRequest) {
        await interceptor({ method, url, headers });
      }
    }

    let lastError: Error | null = null;

    for (let attempt = 0; attempt < maxRetries; attempt++) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);

        // If the caller provides an external abort signal, forward it
        if (options?.signal) {
          if (options.signal.aborted) {
            clearTimeout(timer);
            controller.abort(options.signal.reason);
          } else {
            options.signal.addEventListener("abort", () => {
              controller.abort(options.signal!.reason);
            }, { once: true });
          }
        }

        const response = await fetch(url, {
          method,
          headers,
          body: options?.json
            ? JSON.stringify(options.json)
            : options?.body ?? undefined,
          signal: controller.signal,
        });

        clearTimeout(timer);

        // Run response interceptors (e.g., logging, metrics)
        if (this.config.onResponse) {
          for (const interceptor of this.config.onResponse) {
            await interceptor(response, { method, url });
          }
        }

        return await this.handleResponse<T>(response);
      } catch (e) {
        if (e instanceof RatError) throw e;
        lastError = e as Error;
        if (attempt < maxRetries - 1) {
          await new Promise((r) => setTimeout(r, Math.min(500 * 2 ** attempt, 10_000)));
          continue;
        }
      }
    }

    throw new ConnectionError(lastError?.message ?? "Connection failed");
  }

  private async handleResponse<T>(response: Response): Promise<T> {
    if (response.status >= 400) {
      const contentType = response.headers?.get("content-type") ?? "";
      if (contentType.includes("application/json")) {
        try {
          const body: Record<string, unknown> = await (response.json() as Promise<Record<string, unknown>>);
          // ratd returns {"error": {"code": "...", "type": "...", "message": "..."}}
          // but some endpoints may return {"error": "plain string"}
          let message: string;
          if (typeof body.error === "string") {
            message = body.error;
          } else if (body.error && typeof body.error === "object") {
            const detail = body.error as Record<string, unknown>;
            message = (typeof detail.message === "string" ? detail.message : null)
              ?? (typeof detail.code === "string" ? detail.code : null)
              ?? response.statusText;
          } else {
            message = (typeof body.message === "string" ? body.message : null) ?? response.statusText;
          }
          throw errorFromStatus(response.status, message, body.validation);
        } catch (e) {
          if (e instanceof RatError) throw e;
          // JSON parse failed — fall through to text handling
        }
      }
      const message = (await response.text()) || response.statusText;
      throw errorFromStatus(response.status, message.trim());
    }

    if (response.status === 204) {
      return null as T;
    }

    try {
      return await (response.json() as Promise<T>);
    } catch {
      return (await response.text()) as unknown as T;
    }
  }
}
