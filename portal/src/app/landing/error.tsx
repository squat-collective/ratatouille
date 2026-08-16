"use client";

import { useEffect } from "react";
import { isAuthError } from "@/lib/auth-error";
import { signOut } from "@/lib/auth/client";

export default function LandingError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[rat] Landing error:", error);
  }, [error]);

  if (isAuthError(error)) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="error-block max-w-lg w-full p-6 space-y-4">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-destructive/50">
              {"// landing"}
            </span>
          </div>
          <h2 className="text-sm font-bold tracking-wider text-destructive">
            SESSION EXPIRED
          </h2>
          <p className="text-xs text-muted-foreground font-mono">
            Your session is no longer valid. Please sign in again.
          </p>
          <button
            onClick={() => signOut({ callbackUrl: "/login" })}
            className="text-xs tracking-wider border border-primary px-4 py-2 text-primary hover:bg-primary hover:text-primary-foreground transition-colors font-mono"
          >
            [ SIGN IN ]
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-center min-h-[50vh]">
      <div className="error-block max-w-lg w-full p-6 space-y-4">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-destructive/50">
            {"// landing"}
          </span>
        </div>
        <h2 className="text-sm font-bold tracking-wider text-destructive">
          LANDING ZONE CRASHED
        </h2>
        <p className="text-xs text-muted-foreground font-mono break-all">
          {error.message || "Failed to load landing zones"}
        </p>
        {error.digest && (
          <p className="text-[10px] text-muted-foreground/50 font-mono">
            digest: {error.digest}
          </p>
        )}
        <button
          onClick={reset}
          className="text-xs tracking-wider border border-primary px-4 py-2 text-primary hover:bg-primary hover:text-primary-foreground transition-colors font-mono"
        >
          [ RETRY ]
        </button>
      </div>
    </div>
  );
}
