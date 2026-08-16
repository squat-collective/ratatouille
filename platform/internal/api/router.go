// Package api provides the HTTP API handlers for ratd.
// All endpoints are mounted under /api/v1.
//
// P10-38 TODO: Split this file into separate files for better organization:
//   - router.go:     Server struct, NewRouter, interface definitions
//   - helpers.go:    JSON response helpers (errorJSON, writeJSON, internalError),
//                    pagination (parsePagination, paginate, parseSorting),
//                    error types and constants
//   - middleware.go: ValidatePathParams, securityHeaders, limitJSONBody, validName
//
// Currently all definitions live in this file. The split is deferred to avoid
// merge conflicts with concurrent changes on this branch.
package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/go-chi/cors"
	cloudv1 "github.com/rat-data/rat/platform/gen/cloud/v1"
	"github.com/rat-data/rat/platform/internal/cache"
	"github.com/rat-data/rat/platform/internal/domain"
)

// maxJSONBodySize is the maximum size for JSON request bodies (1MB).
const maxJSONBodySize = 1 << 20

// maxDescriptionLength is the maximum length for description fields (5000 chars).
const maxDescriptionLength = 5000

// maxSQLLength is the maximum length for inline SQL fields in request bodies (500KB).
// The query endpoint has its own maxQueryLength constant (100KB) since interactive
// queries are typically shorter than pipeline SQL definitions.
const maxSQLLength = 500_000

// validNameRe matches lowercase slug resource names: starts with lowercase letter, then lowercase + digits + hyphens + underscores.
var validNameRe = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

// validName returns true if s is a valid lowercase slug (1-128 chars, lowercase letter start, then lowercase + digits + hyphens/underscores).
func validName(s string) bool {
	return len(s) <= 128 && validNameRe.MatchString(s)
}

const (
	defaultPageLimit = 50
	maxPageLimit     = 200
)

// parsePagination reads limit and offset from query params with defaults and bounds.
func parsePagination(r *http.Request) (limit, offset int) {
	limit = defaultPageLimit
	offset = 0
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	if limit > maxPageLimit {
		limit = maxPageLimit
	}
	if v := r.URL.Query().Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}
	return limit, offset
}

// SortOrder represents a sorting directive for list endpoints.
type SortOrder struct {
	Field string // field name to sort by (e.g., "created_at", "name")
	Desc  bool   // true for descending, false for ascending
}

// parseSorting reads sort parameters from query string.
// Format: ?sort=field or ?sort=-field (prefix "-" for descending).
// Returns nil if no sort parameter is provided.
// The allowedFields set restricts which fields can be sorted on to prevent
// injection of arbitrary column names into SQL ORDER BY clauses.
func parseSorting(r *http.Request, allowedFields map[string]bool) *SortOrder {
	sortParam := r.URL.Query().Get("sort")
	if sortParam == "" {
		return nil
	}

	desc := false
	if strings.HasPrefix(sortParam, "-") {
		desc = true
		sortParam = sortParam[1:]
	}

	if !allowedFields[sortParam] {
		return nil // silently ignore unknown sort fields
	}

	return &SortOrder{Field: sortParam, Desc: desc}
}

// Deprecated: paginate applies in-memory offset/limit to a slice. New endpoints should
// push pagination to SQL via Limit/Offset fields on filter structs instead (P2-05).
// Kept for endpoints that have not yet been migrated.
func paginate[T any](items []T, limit, offset int) []T {
	if offset >= len(items) {
		return []T{}
	}
	end := offset + limit
	if end > len(items) {
		end = len(items)
	}
	return items[offset:end]
}

// Structured error type codes for machine-readable error categorization.
// These classify errors into broad categories independent of the HTTP status code.
const (
	ErrorTypeValidation    = "VALIDATION"     // request data failed validation
	ErrorTypeAuthentication = "AUTHENTICATION" // missing or invalid credentials
	ErrorTypeAuthorization = "AUTHORIZATION"   // valid credentials but insufficient permissions
	ErrorTypeNotFound      = "NOT_FOUND"       // requested resource does not exist
	ErrorTypeConflict      = "CONFLICT"        // request conflicts with current resource state
	ErrorTypeRateLimit     = "RATE_LIMIT"      // too many requests
	ErrorTypeInternal      = "INTERNAL"        // unexpected server error
	ErrorTypeUnavailable   = "UNAVAILABLE"     // dependency or feature not available
)

// APIError is the structured JSON error envelope returned by all API error responses.
// Format: {"error": {"code": "ERROR_CODE", "type": "ERROR_TYPE", "message": "human-readable message"}}
type APIError struct {
	Error APIErrorDetail `json:"error"`
}

// APIErrorDetail holds the code, type, and message inside the error envelope.
type APIErrorDetail struct {
	Code    string `json:"code"`
	Type    string `json:"type,omitempty"` // broad error category (VALIDATION, NOT_FOUND, etc.)
	Message string `json:"message"`
}

// errorTypeFromStatus maps HTTP status codes to broad error type categories.
func errorTypeFromStatus(status int) string {
	switch {
	case status == http.StatusBadRequest:
		return ErrorTypeValidation
	case status == http.StatusUnauthorized:
		return ErrorTypeAuthentication
	case status == http.StatusForbidden:
		return ErrorTypeAuthorization
	case status == http.StatusNotFound:
		return ErrorTypeNotFound
	case status == http.StatusConflict:
		return ErrorTypeConflict
	case status == http.StatusTooManyRequests:
		return ErrorTypeRateLimit
	case status == http.StatusServiceUnavailable:
		return ErrorTypeUnavailable
	case status >= 500:
		return ErrorTypeInternal
	default:
		return ""
	}
}

// errorJSON writes a structured JSON error response.
// All API errors use this format so the SDK only needs to handle one shape.
// The type field is automatically derived from the HTTP status code.
func errorJSON(w http.ResponseWriter, message, code string, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(APIError{
		Error: APIErrorDetail{Code: code, Type: errorTypeFromStatus(status), Message: message},
	}); err != nil {
		slog.Error("failed to encode JSON error response", "error", err)
	}
}

// internalError logs the full error server-side and returns a generic JSON error to clients.
func internalError(w http.ResponseWriter, msg string, err error) {
	slog.Error(msg, "error", err)
	errorJSON(w, msg, "INTERNAL", http.StatusInternalServerError)
}

// writeJSON encodes v as JSON and writes it to w with the given status code.
// Logs an error if encoding fails (response may be partial at that point).
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		slog.Error("failed to encode JSON response", "error", err)
	}
}

// limitJSONBody caps request body size for non-multipart requests.
// Upload endpoints (multipart/form-data) manage their own limits via MaxBytesReader.
func limitJSONBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ct := r.Header.Get("Content-Type")
		if r.Body != nil && !strings.HasPrefix(ct, "multipart/") {
			r.Body = http.MaxBytesReader(w, r.Body, maxJSONBodySize)
		}
		next.ServeHTTP(w, r)
	})
}

// nameParams is the set of URL path parameter names that must pass validName().
var nameParams = map[string]bool{
	"namespace": true,
	"ns":        true,
	"name":      true,
	"pipeline":  true,
	"testName":  true,
}

// ValidatePathParams is middleware that validates URL path parameters.
// Name-like params must be valid lowercase slugs; "layer" must be a valid layer.
// Other params (UUIDs, wildcards, etc.) are skipped.
func ValidatePathParams(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rctx := chi.RouteContext(r.Context())
		if rctx != nil {
			for i, key := range rctx.URLParams.Keys {
				val := rctx.URLParams.Values[i]
				if val == "" {
					continue
				}
				if nameParams[key] {
					if !validName(val) {
						errorJSON(w, key+" must be a lowercase slug (a-z, 0-9, hyphens, underscores; must start with a letter)", "INVALID_ARGUMENT", http.StatusBadRequest)
						return
					}
				} else if key == "layer" {
					if !domain.ValidLayer(val) {
						errorJSON(w, "layer must be bronze, silver, or gold", "INVALID_ARGUMENT", http.StatusBadRequest)
						return
					}
				}
			}
		}
		next.ServeHTTP(w, r)
	})
}

// PluginRegistry provides dynamic feature detection based on loaded plugins.
// The plugins package implements this interface.
type PluginRegistry interface {
	Features() domain.Features
}

// CloudProvider vends scoped cloud credentials for pipeline runs.
// Implemented by the plugins.Registry when the cloud plugin is loaded.
type CloudProvider interface {
	CloudEnabled() bool
	GetCredentials(ctx context.Context, userID, namespace string) (*cloudv1.GetCredentialsResponse, error)
}

// securityHeaders adds standard HTTP security headers to every response.
func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("X-XSS-Protection", "0") // modern browsers: CSP replaces this
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		next.ServeHTTP(w, r)
	})
}

// WebhookRateLimitConfig configures the rate limiter for the webhook endpoint.
// Separate from RateLimitConfig because webhooks need tighter limits (they are
// externally callable without JWT auth, authenticated only by token).
type WebhookRateLimitConfig struct {
	RequestsPerSecond float64       // Token refill rate (e.g. 1.67 = 100 req/min)
	Burst             int           // Max burst size
	CleanupInterval   time.Duration // How often to evict stale entries
}

// DefaultWebhookRateLimitConfig returns defaults suitable for webhooks (100 req/min, burst 20).
func DefaultWebhookRateLimitConfig() WebhookRateLimitConfig {
	return WebhookRateLimitConfig{
		RequestsPerSecond: 100.0 / 60.0, // ~1.67 req/s = 100 req/min
		Burst:             20,
		CleanupInterval:   5 * time.Minute,
	}
}

// Server holds dependencies for all API handlers.
type Server struct {
	Pipelines     PipelineStore
	Versions      VersionStore
	Publisher     PipelinePublisher // Optional: wraps publish/rollback in a DB transaction.
	Runs          RunStore
	Namespaces    NamespaceStore
	Schedules     ScheduleStore
	Storage       StorageStore
	Quality       QualityStore
	Query         QueryStore
	TableMetadata TableMetadataStore
	LandingZones  LandingZoneStore
	Triggers      PipelineTriggerStore
	Audit         AuditStore
	Settings      SettingsStore
	Auth          func(http.Handler) http.Handler
	Authorizer    Authorizer
	Executor      Executor
	Reaper        ReaperRunner
	Plugins       PluginRegistry
	Cloud         CloudProvider
	LicenseInfo   *domain.LicenseInfo
	CORSOrigins   []string          // Allowed CORS origins. Defaults to ["http://localhost:3000"].
	RateLimit        *RateLimitConfig   // Per-IP rate limiting config. Nil disables rate limiting.
	RateLimiterStop  func()            // Populated by NewRouter when rate limiting is enabled.
	WebhookRateLimit *WebhookRateLimitConfig // Per-IP webhook rate limiting. Nil = uses default config.
	WebhookRateLimiterStop func()            // Populated by NewRouter for webhook rate limiter cleanup.
	SSELimiter       *SSELimiter       // Concurrent SSE connection limiter. Nil = uses a default limiter.
	DBHealth         HealthChecker     // Postgres health check (pool.Ping). Nil = skip.
	S3Health         HealthChecker     // S3/MinIO health check (BucketExists). Nil = skip.
	RunnerHealth     HealthChecker     // Runner gRPC health check. Nil = skip.
	QueryHealth      HealthChecker     // ratq gRPC health check. Nil = skip.

	// Caches reduce Postgres load for slow-changing data.
	// Nil caches are safe — handlers check before using.
	NamespaceCache *cache.Cache[string, []domain.Namespace]   // key: "all" (namespace list rarely changes)
	PipelineCache  *cache.Cache[string, *domain.Pipeline]     // key: "ns/layer/name"
}

// NewRouter creates a configured chi router with all API routes mounted.
func NewRouter(srv *Server) chi.Router {
	// Ensure SSE limiter is always available.
	if srv.SSELimiter == nil {
		srv.SSELimiter = NewSSELimiter()
	}

	r := chi.NewRouter()

	// Middleware
	corsOrigins := srv.CORSOrigins
	if len(corsOrigins) == 0 {
		corsOrigins = []string{"http://localhost:3000"}
	}

	// P10-20: When AllowCredentials is true, Access-Control-Allow-Origin MUST NOT
	// be "*". If the caller configured "*", use AllowOriginFunc to dynamically
	// reflect the request Origin header (only when it matches a known allowed
	// origin pattern). This satisfies the CORS spec while keeping credentials.
	hasWildcard := false
	for _, o := range corsOrigins {
		if o == "*" {
			hasWildcard = true
			break
		}
	}

	corsOpts := cors.Options{
		AllowedMethods:   []string{"GET", "POST", "PUT", "DELETE", "OPTIONS"},
		AllowedHeaders:   []string{"Accept", "Authorization", "Content-Type", "X-Webhook-Token", "X-Request-ID"},
		ExposedHeaders:   []string{"Link", "X-Request-ID", "RateLimit-Limit", "RateLimit-Remaining", "Retry-After"},
		AllowCredentials: true,
		MaxAge:           300,
	}

	if hasWildcard {
		// Dynamic origin: reflect the request Origin when credentials are enabled.
		// This avoids the browser-rejected "Access-Control-Allow-Origin: *" +
		// "Access-Control-Allow-Credentials: true" combination.
		slog.Warn("CORS: wildcard origin '*' with AllowCredentials — using dynamic origin reflection")
		corsOpts.AllowOriginFunc = func(_ *http.Request, _ string) bool {
			return true
		}
	} else {
		corsOpts.AllowedOrigins = corsOrigins
	}

	r.Use(cors.Handler(corsOpts))
	r.Use(securityHeaders)
	r.Use(RequestID)
	r.Use(middleware.RealIP)
	r.Use(RequestLogger)
	r.Use(middleware.Recoverer)

	// Health, metrics & features (unauthenticated, outside /api/v1 auth)
	r.Get("/health", srv.HandleHealth)
	r.Get("/health/live", srv.HandleHealthLive)
	r.Get("/health/ready", srv.HandleHealthReady)
	r.Get("/metrics", srv.HandleMetrics)
	r.Get("/api/v1/features", srv.HandleFeatures)

	// Webhooks (token-authenticated, no JWT required).
	// Rate-limited separately from the main API because webhooks are externally
	// callable without JWT auth, making them a higher DoS risk.
	if srv.Triggers != nil {
		webhookCfg := DefaultWebhookRateLimitConfig()
		if srv.WebhookRateLimit != nil {
			webhookCfg = *srv.WebhookRateLimit
		}
		wrl, wmw := RateLimit(RateLimitConfig(webhookCfg))
		srv.WebhookRateLimiterStop = wrl.Stop
		r.Group(func(r chi.Router) {
			r.Use(wmw)
			MountWebhookRoutes(r, srv)
		})
	}

	// Internal service-to-service routes (no JWT required).
	// Called by runner/plugins for push-based status updates.
	MountInternalRoutes(r, srv)

	// API v1
	r.Route("/api/v1", func(r chi.Router) {
		r.Use(limitJSONBody)
		if srv.RateLimit != nil {
			rl, mw := RateLimit(*srv.RateLimit)
			srv.RateLimiterStop = rl.Stop
			r.Use(mw)
		}
		if srv.Auth != nil {
			r.Use(srv.Auth)
		}
		if srv.Audit != nil {
			r.Use(AuditMiddleware(srv.Audit))
		}
		r.Get("/me", srv.HandleMe)

		// ValidatePathParams needs URL params, which are only available after
		// chi matches the specific route. r.With() creates an inline router where
		// middleware wraps each handler (runs post-match), unlike r.Use() which
		// wraps routeHTTP (runs pre-match).
		vr := r.With(ValidatePathParams)
		MountPipelineRoutes(vr, srv)
		MountRunRoutes(vr, srv)
		MountNamespaceRoutes(vr, srv)
		MountScheduleRoutes(vr, srv)
		MountStorageRoutes(vr, srv)
		MountQualityRoutes(vr, srv)
		MountMetadataRoutes(vr, srv)
		MountQueryRoutes(vr, srv)
		MountLineageRoutes(vr, srv)
		MountSharingRoutes(vr, srv)
		MountLandingZoneRoutes(vr, srv)
		if srv.Triggers != nil {
			MountTriggerRoutes(vr, srv)
		}
		MountAuditRoutes(vr, srv)
		MountPreviewRoutes(vr, srv)
		MountPublishRoutes(vr, srv)
		if srv.Settings != nil {
			MountRetentionRoutes(vr, srv)
		}
		if srv.Versions != nil {
			MountVersionRoutes(vr, srv)
		}
		if pp := srv.permissionProvider(); pp != nil && pp.PermissionEnabled() {
			MountPermissionRoutes(vr, srv)
		}
		if ip := srv.identityProvider(); ip != nil && ip.IdentityEnabled() {
			MountIdentityRoutes(vr, srv)
		}
	})

	return r
}
