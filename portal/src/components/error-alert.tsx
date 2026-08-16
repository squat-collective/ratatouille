"use client";

import { AlertTriangle } from "lucide-react";
import { isAuthError } from "@/lib/auth-error";
import { signOut } from "@/lib/auth/client";

interface ErrorAlertProps {
  /** Error message to display. Accepts Error objects or strings. */
  error: Error | string | unknown;
  /** Optional prefix text before the error message. */
  prefix?: string;
}

/**
 * Displays an API/SWR error in the RAT underground aesthetic.
 * Uses the existing error-block CSS class for consistent styling
 * with the scanning red line animation.
 *
 * Auth errors show a "Session expired" message with a SIGN IN button.
 */
export function ErrorAlert({ error, prefix }: ErrorAlertProps) {
  if (isAuthError(error)) {
    return (
      <div className="error-block px-4 py-3 flex items-start gap-2">
        <AlertTriangle className="h-3.5 w-3.5 text-destructive shrink-0 mt-0.5" />
        <div className="text-xs text-destructive flex items-center gap-3">
          <span>Session expired</span>
          <button
            onClick={() => signOut({ callbackUrl: "/login" })}
            className="tracking-wider border border-destructive px-2 py-0.5 hover:bg-destructive hover:text-destructive-foreground transition-colors font-mono"
          >
            SIGN IN
          </button>
        </div>
      </div>
    );
  }

  // Duck-type message extraction — instanceof Error can fail across
  // webpack module boundaries, so check .message property directly.
  let message = "An unexpected error occurred";
  if (typeof error === "string") {
    message = error;
  } else if (error && typeof error === "object") {
    const obj = error as Record<string, unknown>;
    if (typeof obj.message === "string") message = obj.message;
    else if (typeof obj.error === "string") message = obj.error;
  }

  return (
    <div className="error-block px-4 py-3 flex items-start gap-2">
      <AlertTriangle className="h-3.5 w-3.5 text-destructive shrink-0 mt-0.5" />
      <div className="text-xs text-destructive">
        {prefix && <span className="font-bold tracking-wider">{prefix}: </span>}
        {message}
      </div>
    </div>
  );
}
