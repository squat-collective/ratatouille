"use client";

import { RatClient } from "@squat-collective/rat-client";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";
import { useAuthSession, signOut } from "@/lib/auth/client";
import { SWRConfig } from "swr";
import { PUBLIC_API_URL } from "@/lib/api-client";
import { isAuthError } from "@/lib/auth-error";

const ApiContext = createContext<RatClient | null>(null);

export function ApiProvider({ children }: { children: React.ReactNode }) {
  const { data: session, status } = useAuthSession();
  const accessToken = session?.accessToken;
  const signingOut = useRef(false);

  const triggerSignOut = () => {
    if (signingOut.current || status !== "authenticated") return;
    signingOut.current = true;
    signOut({ callbackUrl: "/login" });
  };

  // Server-side token refresh failed (Keycloak restart, revoked session, etc.)
  // This is the ONLY automatic signOut trigger — it fires when the NextAuth
  // JWT callback confirms the refresh token is dead, not on transient 401s.
  useEffect(() => {
    if (session?.error === "RefreshTokenError") {
      triggerSignOut();
    }
  }, [session?.error, status]);

  const client = useMemo(
    () =>
      new RatClient({
        apiUrl: PUBLIC_API_URL,
        // When an access token is present (pro auth), inject Bearer header
        ...(accessToken
          ? {
              onRequest: [
                (req) => {
                  req.headers["Authorization"] = `Bearer ${accessToken}`;
                },
              ],
            }
          : {}),
      }),
    [accessToken],
  );

  // Don't render children until auth status is resolved.
  // Without this gate, SWR hooks fire during "loading" (no token yet),
  // get 401s, and cache them permanently (onErrorRetry skips auth errors).
  // Community edition always returns "unauthenticated" (never "loading").
  if (status === "loading") return null;

  return (
    <SWRConfig
      value={{
        onError: (err) => {
          // Auth errors are handled by ErrorAlert (shows SIGN IN button)
          // and by the session.error useEffect above (auto signOut on
          // confirmed refresh failure). No automatic signOut here to
          // avoid nuking valid sessions on transient 401s.
          if (!isAuthError(err)) {
            console.error("[SWR]", err);
          }
        },
        onErrorRetry: (err, _key, _config, revalidate, { retryCount }) => {
          // Don't retry auth errors — the token is stale, retrying is pointless
          if (isAuthError(err)) return;
          // Default SWR retry for other errors
          setTimeout(() => revalidate({ retryCount }), 5000 * 2 ** retryCount);
        },
      }}
    >
      <ApiContext.Provider value={client}>{children}</ApiContext.Provider>
    </SWRConfig>
  );
}

export function useApiClient(): RatClient {
  const ctx = useContext(ApiContext);
  if (!ctx) throw new Error("useApiClient must be inside ApiProvider");
  return ctx;
}
