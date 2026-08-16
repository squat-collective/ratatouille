package plugins

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	connect "connectrpc.com/connect"
	authv1 "github.com/rat-data/rat/platform/gen/auth/v1"
	"github.com/rat-data/rat/platform/gen/auth/v1/authv1connect"
	cloudv1 "github.com/rat-data/rat/platform/gen/cloud/v1"
	"github.com/rat-data/rat/platform/gen/cloud/v1/cloudv1connect"
	enforcementv1 "github.com/rat-data/rat/platform/gen/enforcement/v1"
	"github.com/rat-data/rat/platform/gen/enforcement/v1/enforcementv1connect"
	"github.com/rat-data/rat/platform/gen/executor/v1/executorv1connect"
	identityv1 "github.com/rat-data/rat/platform/gen/identity/v1"
	"github.com/rat-data/rat/platform/gen/identity/v1/identityv1connect"
	permissionv1 "github.com/rat-data/rat/platform/gen/permission/v1"
	"github.com/rat-data/rat/platform/gen/permission/v1/permissionv1connect"
	pluginv1 "github.com/rat-data/rat/platform/gen/plugin/v1"
	"github.com/rat-data/rat/platform/gen/plugin/v1/pluginv1connect"
	sharingv1 "github.com/rat-data/rat/platform/gen/sharing/v1"
	"github.com/rat-data/rat/platform/gen/sharing/v1/sharingv1connect"
	"github.com/rat-data/rat/platform/internal/config"
	"github.com/rat-data/rat/platform/internal/domain"
)

const healthCheckTimeout = 5 * time.Second

// Known plugin names that ratd understands.
const (
	PluginAuth        = "auth"
	PluginExecutor    = "executor"
	PluginSharing     = "sharing"
	PluginEnforcement = "enforcement"
	PluginCloud       = "cloud"
	PluginPermission  = "permission"
	PluginIdentity    = "identity"
)

// Registry holds connected and healthy plugin clients.
// An empty Registry represents the community edition (no plugins).
type Registry struct {
	edition     string
	auth        *authPlugin
	executor    *executorPlugin
	sharing     *sharingPlugin
	enforcement *enforcementPlugin
	cloud       *cloudPlugin
	permission  *permissionPlugin
	identity    *identityPlugin
}

// sharingPlugin wraps the sharing ConnectRPC client.
type sharingPlugin struct {
	client sharingv1connect.SharingServiceClient
}

// enforcementPlugin wraps the enforcement ConnectRPC client.
type enforcementPlugin struct {
	client enforcementv1connect.EnforcementServiceClient
}

// authPlugin wraps the auth ConnectRPC client.
type authPlugin struct {
	client authv1connect.AuthServiceClient
}

// executorPlugin wraps the executor ConnectRPC client.
type executorPlugin struct {
	client executorv1connect.ExecutorServiceClient
	addr   string
}

// cloudPlugin wraps the cloud ConnectRPC client.
type cloudPlugin struct {
	client cloudv1connect.CloudServiceClient
}

// permissionPlugin wraps the permission ConnectRPC client.
type permissionPlugin struct {
	client permissionv1connect.PermissionServiceClient
}

// identityPlugin wraps the identity ConnectRPC client.
type identityPlugin struct {
	client identityv1connect.IdentityServiceClient
}

// healthChecker is the interface for plugin health checks.
// Every plugin container must implement PluginService.HealthCheck.
type healthChecker interface {
	HealthCheck(context.Context, *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error)
}

// pluginClientFactory creates ConnectRPC clients for testing.
type pluginClientFactory struct {
	newPluginClient      func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) pluginv1connect.PluginServiceClient
	newAuthClient        func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) authv1connect.AuthServiceClient
	newExecutorClient    func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) executorv1connect.ExecutorServiceClient
	newSharingClient     func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) sharingv1connect.SharingServiceClient
	newEnforcementClient func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) enforcementv1connect.EnforcementServiceClient
	newCloudClient       func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) cloudv1connect.CloudServiceClient
	newPermissionClient  func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) permissionv1connect.PermissionServiceClient
	newIdentityClient    func(httpClient connect.HTTPClient, baseURL string, opts ...connect.ClientOption) identityv1connect.IdentityServiceClient
}

// defaultFactory creates real ConnectRPC clients.
func defaultFactory() *pluginClientFactory {
	return &pluginClientFactory{
		newPluginClient:      pluginv1connect.NewPluginServiceClient,
		newAuthClient:        authv1connect.NewAuthServiceClient,
		newExecutorClient:    executorv1connect.NewExecutorServiceClient,
		newSharingClient:     sharingv1connect.NewSharingServiceClient,
		newEnforcementClient: enforcementv1connect.NewEnforcementServiceClient,
		newCloudClient:       cloudv1connect.NewCloudServiceClient,
		newPermissionClient:  permissionv1connect.NewPermissionServiceClient,
		newIdentityClient:    identityv1connect.NewIdentityServiceClient,
	}
}

// Load connects to all configured plugins, health-checks them, and returns a Registry.
// Unhealthy plugins are logged as warnings and disabled.
// Unknown plugin names are skipped with a warning.
// Pass an optional HTTP client for TLS transport; defaults to http.DefaultClient.
func Load(ctx context.Context, cfg *config.Config, httpClient ...*http.Client) (*Registry, error) {
	var c *http.Client
	if len(httpClient) > 0 && httpClient[0] != nil {
		c = httpClient[0]
	}
	if c == nil {
		c = http.DefaultClient
	}
	return loadWithFactory(ctx, cfg, defaultFactory(), c)
}

// loadWithFactory is the internal implementation that accepts a client factory for testing.
func loadWithFactory(ctx context.Context, cfg *config.Config, factory *pluginClientFactory, httpClient *http.Client) (*Registry, error) {
	reg := &Registry{
		edition: cfg.Edition,
	}

	for name, pluginCfg := range cfg.Plugins {
		switch name {
		case PluginAuth:
			if err := reg.loadAuth(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("auth plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginExecutor:
			if err := reg.loadExecutor(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("executor plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginSharing:
			if err := reg.loadSharing(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("sharing plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginEnforcement:
			if err := reg.loadEnforcement(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("enforcement plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginCloud:
			if err := reg.loadCloud(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("cloud plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginPermission:
			if err := reg.loadPermission(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("permission plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		case PluginIdentity:
			if err := reg.loadIdentity(ctx, pluginCfg, factory, httpClient); err != nil {
				slog.Warn("identity plugin unhealthy, disabled", "addr", pluginCfg.Addr, "error", err)
			}
		default:
			slog.Warn("unknown plugin, skipped", "name", name)
		}
	}

	return reg, nil
}

// loadAuth connects to the auth plugin, health-checks it, and stores the client.
func (r *Registry) loadAuth(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	// Health check first
	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	// Version negotiation — warn on mismatches but don't block loading.
	if err := CheckVersionFromHealthMessage(PluginAuth, healthMsg); err != nil {
		slog.Warn("auth plugin version negotiation issue", "error", err)
	}

	// Healthy — create the auth client
	authClient := factory.newAuthClient(httpClient, addr)
	r.auth = &authPlugin{client: authClient}

	slog.Info("auth plugin loaded", "addr", cfg.Addr)
	return nil
}

// checkHealth calls PluginService.HealthCheck with a timeout.
// Returns the health check response message (used for version negotiation).
func checkHealth(ctx context.Context, client healthChecker) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, healthCheckTimeout)
	defer cancel()

	resp, err := client.HealthCheck(ctx, connect.NewRequest(&pluginv1.HealthCheckRequest{}))
	if err != nil {
		return "", fmt.Errorf("health check failed: %w", err)
	}

	if resp.Msg.Status != pluginv1.Status_STATUS_SERVING {
		return "", fmt.Errorf("plugin not serving: %s", resp.Msg.Message)
	}

	return resp.Msg.Message, nil
}

// Features returns the dynamic feature set based on loaded plugins.
// This replaces the hardcoded features in health.go.
func (r *Registry) Features() domain.Features {
	executorType := "warmpool"
	if r.executor != nil {
		executorType = "container"
	}
	features := domain.Features{
		Edition:    r.edition,
		Namespaces: r.edition != "community",
		MultiUser:  r.auth != nil,
		Plugins: map[string]domain.PluginFeature{
			"auth":        {Enabled: r.auth != nil},
			"sharing":     {Enabled: r.sharing != nil},
			"executor":    {Enabled: true, Type: executorType},
			"audit":       {Enabled: false},
			"enforcement": {Enabled: r.enforcement != nil},
			"cloud":       {Enabled: r.cloud != nil},
			"permission":  {Enabled: r.permission != nil},
			"identity":    {Enabled: r.identity != nil},
		},
	}
	return features
}

// AuthEnabled returns true if the auth plugin is loaded and healthy.
func (r *Registry) AuthEnabled() bool {
	return r.auth != nil
}

// Authenticate delegates to the auth plugin's Authenticate RPC.
// Only call this if AuthEnabled() returns true.
func (r *Registry) Authenticate(ctx context.Context, token string) (*authv1.AuthenticateResponse, error) {
	if r.auth == nil {
		return nil, fmt.Errorf("auth plugin not loaded")
	}

	resp, err := r.auth.client.Authenticate(ctx, connect.NewRequest(&authv1.AuthenticateRequest{
		Token: token,
	}))
	if err != nil {
		return nil, fmt.Errorf("authenticate: %w", err)
	}

	return resp.Msg, nil
}

// loadExecutor connects to the executor plugin, health-checks it, and stores the client.
func (r *Registry) loadExecutor(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	// Health check first
	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginExecutor, healthMsg); err != nil {
		slog.Warn("executor plugin version negotiation issue", "error", err)
	}

	// Healthy — create the executor client
	execClient := factory.newExecutorClient(httpClient, addr)
	r.executor = &executorPlugin{client: execClient, addr: addr}

	slog.Info("executor plugin loaded", "addr", cfg.Addr)
	return nil
}

// ExecutorEnabled returns true if the executor plugin is loaded and healthy.
func (r *Registry) ExecutorEnabled() bool {
	return r.executor != nil
}

// GetExecutorAddr returns the executor plugin's address for creating a PluginExecutor.
func (r *Registry) GetExecutorAddr() string {
	if r.executor == nil {
		return ""
	}
	return r.executor.addr
}

// loadSharing connects to the sharing plugin, health-checks it, and stores the client.
func (r *Registry) loadSharing(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginSharing, healthMsg); err != nil {
		slog.Warn("sharing plugin version negotiation issue", "error", err)
	}

	sharingClient := factory.newSharingClient(httpClient, addr)
	r.sharing = &sharingPlugin{client: sharingClient}

	slog.Info("sharing plugin loaded", "addr", cfg.Addr)
	return nil
}

// loadEnforcement connects to the enforcement plugin, health-checks it, and stores the client.
func (r *Registry) loadEnforcement(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginEnforcement, healthMsg); err != nil {
		slog.Warn("enforcement plugin version negotiation issue", "error", err)
	}

	enfClient := factory.newEnforcementClient(httpClient, addr)
	r.enforcement = &enforcementPlugin{client: enfClient}

	slog.Info("enforcement plugin loaded", "addr", cfg.Addr)
	return nil
}

// SharingEnabled returns true if the sharing plugin is loaded and healthy.
func (r *Registry) SharingEnabled() bool {
	return r.sharing != nil
}

// EnforcementEnabled returns true if the enforcement plugin is loaded and healthy.
func (r *Registry) EnforcementEnabled() bool {
	return r.enforcement != nil
}

// ShareResource delegates to the sharing plugin's ShareResource RPC.
func (r *Registry) ShareResource(ctx context.Context, grantorID, granteeID, resourceType, resourceID, permission string) (*sharingv1.ShareResourceResponse, error) {
	if r.sharing == nil {
		return nil, fmt.Errorf("sharing plugin not loaded")
	}

	permEnum := sharingv1.Permission_PERMISSION_READ
	switch permission {
	case "write":
		permEnum = sharingv1.Permission_PERMISSION_WRITE
	case "admin":
		permEnum = sharingv1.Permission_PERMISSION_ADMIN
	}

	resp, err := r.sharing.client.ShareResource(ctx, connect.NewRequest(&sharingv1.ShareResourceRequest{
		GrantorId:    grantorID,
		GranteeId:    granteeID,
		ResourceType: resourceType,
		ResourceId:   resourceID,
		Permission:   permEnum,
	}))
	if err != nil {
		return nil, fmt.Errorf("share resource: %w", err)
	}
	return resp.Msg, nil
}

// RevokeAccess delegates to the sharing plugin's RevokeAccess RPC.
func (r *Registry) RevokeAccess(ctx context.Context, grantID, revokedBy string) error {
	if r.sharing == nil {
		return fmt.Errorf("sharing plugin not loaded")
	}

	_, err := r.sharing.client.RevokeAccess(ctx, connect.NewRequest(&sharingv1.RevokeAccessRequest{
		GrantId:   grantID,
		RevokedBy: revokedBy,
	}))
	if err != nil {
		return fmt.Errorf("revoke access: %w", err)
	}
	return nil
}

// ListAccess delegates to the sharing plugin's ListAccess RPC.
func (r *Registry) ListAccess(ctx context.Context, resourceType, resourceID string) (*sharingv1.ListAccessResponse, error) {
	if r.sharing == nil {
		return nil, fmt.Errorf("sharing plugin not loaded")
	}

	resp, err := r.sharing.client.ListAccess(ctx, connect.NewRequest(&sharingv1.ListAccessRequest{
		ResourceType: resourceType,
		ResourceId:   resourceID,
	}))
	if err != nil {
		return nil, fmt.Errorf("list access: %w", err)
	}
	return resp.Msg, nil
}

// CanAccess delegates to the enforcement plugin's CanAccess RPC.
func (r *Registry) CanAccess(ctx context.Context, userID, resourceType, resourceID, action string) (bool, error) {
	if r.enforcement == nil {
		return false, fmt.Errorf("enforcement plugin not loaded")
	}

	resp, err := r.enforcement.client.CanAccess(ctx, connect.NewRequest(&enforcementv1.CanAccessRequest{
		UserId:       userID,
		ResourceType: resourceType,
		ResourceId:   resourceID,
		Action:       action,
	}))
	if err != nil {
		return false, fmt.Errorf("can access: %w", err)
	}
	return resp.Msg.Allowed, nil
}

// loadCloud connects to the cloud plugin, health-checks it, and stores the client.
func (r *Registry) loadCloud(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginCloud, healthMsg); err != nil {
		slog.Warn("cloud plugin version negotiation issue", "error", err)
	}

	cloudClient := factory.newCloudClient(httpClient, addr)
	r.cloud = &cloudPlugin{client: cloudClient}

	slog.Info("cloud plugin loaded", "addr", cfg.Addr)
	return nil
}

// CloudEnabled returns true if the cloud plugin is loaded and healthy.
func (r *Registry) CloudEnabled() bool {
	return r.cloud != nil
}

// GetCredentials delegates to the cloud plugin's GetCredentials RPC.
func (r *Registry) GetCredentials(ctx context.Context, userID, namespace string) (*cloudv1.GetCredentialsResponse, error) {
	if r.cloud == nil {
		return nil, fmt.Errorf("cloud plugin not loaded")
	}

	resp, err := r.cloud.client.GetCredentials(ctx, connect.NewRequest(&cloudv1.GetCredentialsRequest{
		UserId:    userID,
		Namespace: namespace,
	}))
	if err != nil {
		return nil, fmt.Errorf("get credentials: %w", err)
	}

	return resp.Msg, nil
}

// loadPermission connects to the permission plugin, health-checks it, and stores the client.
func (r *Registry) loadPermission(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginPermission, healthMsg); err != nil {
		slog.Warn("permission plugin version negotiation issue", "error", err)
	}

	permClient := factory.newPermissionClient(httpClient, addr)
	r.permission = &permissionPlugin{client: permClient}

	slog.Info("permission plugin loaded", "addr", cfg.Addr)
	return nil
}

// PermissionEnabled returns true if the permission plugin is loaded and healthy.
func (r *Registry) PermissionEnabled() bool {
	return r.permission != nil
}

// ListVerbs delegates to the permission plugin's ListVerbs RPC.
func (r *Registry) ListVerbs(ctx context.Context) (*permissionv1.ListVerbsResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListVerbs(ctx, connect.NewRequest(&permissionv1.ListVerbsRequest{}))
	if err != nil {
		return nil, fmt.Errorf("list verbs: %w", err)
	}
	return resp.Msg, nil
}

// RegisterVerb delegates to the permission plugin's RegisterVerb RPC.
func (r *Registry) RegisterVerb(ctx context.Context, name string, implies []string, description string) error {
	if r.permission == nil {
		return fmt.Errorf("permission plugin not loaded")
	}
	_, err := r.permission.client.RegisterVerb(ctx, connect.NewRequest(&permissionv1.RegisterVerbRequest{
		Name:        name,
		Implies:     implies,
		Description: description,
	}))
	if err != nil {
		return fmt.Errorf("register verb: %w", err)
	}
	return nil
}

// ListGrants delegates to the permission plugin's ListGrants RPC.
func (r *Registry) ListGrants(ctx context.Context, resource string, principalType permissionv1.PrincipalType, principalID string) (*permissionv1.ListGrantsResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListGrants(ctx, connect.NewRequest(&permissionv1.ListGrantsRequest{
		Resource:      resource,
		PrincipalType: principalType,
		PrincipalId:   principalID,
	}))
	if err != nil {
		return nil, fmt.Errorf("list grants: %w", err)
	}
	return resp.Msg, nil
}

// CreatePermissionGrant delegates to the permission plugin's CreateGrant RPC.
func (r *Registry) CreatePermissionGrant(ctx context.Context, req *permissionv1.CreateGrantRequest) (*permissionv1.CreateGrantResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.CreateGrant(ctx, connect.NewRequest(req))
	if err != nil {
		return nil, fmt.Errorf("create grant: %w", err)
	}
	return resp.Msg, nil
}

// RevokePermissionGrant delegates to the permission plugin's RevokeGrant RPC.
func (r *Registry) RevokePermissionGrant(ctx context.Context, grantID string) (*permissionv1.RevokeGrantResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.RevokeGrant(ctx, connect.NewRequest(&permissionv1.RevokeGrantRequest{
		GrantId: grantID,
	}))
	if err != nil {
		return nil, fmt.Errorf("revoke grant: %w", err)
	}
	return resp.Msg, nil
}

// ListGroups delegates to the permission plugin's ListGroups RPC.
func (r *Registry) ListGroups(ctx context.Context) (*permissionv1.ListGroupsResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListGroups(ctx, connect.NewRequest(&permissionv1.ListGroupsRequest{}))
	if err != nil {
		return nil, fmt.Errorf("list groups: %w", err)
	}
	return resp.Msg, nil
}

// CreatePermissionGroup delegates to the permission plugin's CreateGroup RPC.
func (r *Registry) CreatePermissionGroup(ctx context.Context, name, description string) (*permissionv1.CreateGroupResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.CreateGroup(ctx, connect.NewRequest(&permissionv1.CreateGroupRequest{
		Name:        name,
		Description: description,
	}))
	if err != nil {
		return nil, fmt.Errorf("create group: %w", err)
	}
	return resp.Msg, nil
}

// DeletePermissionGroup delegates to the permission plugin's DeleteGroup RPC.
func (r *Registry) DeletePermissionGroup(ctx context.Context, groupID string) (*permissionv1.DeleteGroupResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.DeleteGroup(ctx, connect.NewRequest(&permissionv1.DeleteGroupRequest{
		GroupId: groupID,
	}))
	if err != nil {
		return nil, fmt.Errorf("delete group: %w", err)
	}
	return resp.Msg, nil
}

// ListGroupMembers delegates to the permission plugin's ListGroupMembers RPC.
func (r *Registry) ListGroupMembers(ctx context.Context, groupID string) (*permissionv1.ListGroupMembersResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListGroupMembers(ctx, connect.NewRequest(&permissionv1.ListGroupMembersRequest{
		GroupId: groupID,
	}))
	if err != nil {
		return nil, fmt.Errorf("list group members: %w", err)
	}
	return resp.Msg, nil
}

// AddGroupMember delegates to the permission plugin's AddGroupMember RPC.
func (r *Registry) AddGroupMember(ctx context.Context, groupID string, memberType permissionv1.PrincipalType, memberID string) error {
	if r.permission == nil {
		return fmt.Errorf("permission plugin not loaded")
	}
	_, err := r.permission.client.AddGroupMember(ctx, connect.NewRequest(&permissionv1.AddGroupMemberRequest{
		GroupId:    groupID,
		MemberType: memberType,
		MemberId:   memberID,
	}))
	if err != nil {
		return fmt.Errorf("add group member: %w", err)
	}
	return nil
}

// RemoveGroupMember delegates to the permission plugin's RemoveGroupMember RPC.
func (r *Registry) RemoveGroupMember(ctx context.Context, groupID string, memberType permissionv1.PrincipalType, memberID string) (*permissionv1.RemoveGroupMemberResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.RemoveGroupMember(ctx, connect.NewRequest(&permissionv1.RemoveGroupMemberRequest{
		GroupId:    groupID,
		MemberType: memberType,
		MemberId:   memberID,
	}))
	if err != nil {
		return nil, fmt.Errorf("remove group member: %w", err)
	}
	return resp.Msg, nil
}

// CheckPermissionAccess delegates to the permission plugin's CheckAccess RPC.
func (r *Registry) CheckPermissionAccess(ctx context.Context, userID string, userGroups []string, resource, verb string) (*permissionv1.CheckAccessResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.CheckAccess(ctx, connect.NewRequest(&permissionv1.CheckAccessRequest{
		UserId:     userID,
		UserGroups: userGroups,
		Resource:   resource,
		Verb:       verb,
	}))
	if err != nil {
		return nil, fmt.Errorf("check access: %w", err)
	}
	return resp.Msg, nil
}

// ListResourceAccess delegates to the permission plugin's ListResourceAccess RPC.
func (r *Registry) ListResourceAccess(ctx context.Context, resource string) (*permissionv1.ListResourceAccessResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListResourceAccess(ctx, connect.NewRequest(&permissionv1.ListResourceAccessRequest{
		Resource: resource,
	}))
	if err != nil {
		return nil, fmt.Errorf("list resource access: %w", err)
	}
	return resp.Msg, nil
}

// ListPrincipalAccess delegates to the permission plugin's ListPrincipalAccess RPC.
func (r *Registry) ListPrincipalAccess(ctx context.Context, userID string, userGroups []string, resourcePrefix string) (*permissionv1.ListPrincipalAccessResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.ListPrincipalAccess(ctx, connect.NewRequest(&permissionv1.ListPrincipalAccessRequest{
		UserId:         userID,
		UserGroups:     userGroups,
		ResourcePrefix: resourcePrefix,
	}))
	if err != nil {
		return nil, fmt.Errorf("list principal access: %w", err)
	}
	return resp.Msg, nil
}

// RemovePermissionResource delegates to the permission plugin's RemoveResource RPC.
func (r *Registry) RemovePermissionResource(ctx context.Context, resource string, cascade bool) (*permissionv1.RemoveResourceResponse, error) {
	if r.permission == nil {
		return nil, fmt.Errorf("permission plugin not loaded")
	}
	resp, err := r.permission.client.RemoveResource(ctx, connect.NewRequest(&permissionv1.RemoveResourceRequest{
		Resource: resource,
		Cascade:  cascade,
	}))
	if err != nil {
		return nil, fmt.Errorf("remove resource: %w", err)
	}
	return resp.Msg, nil
}

// loadIdentity connects to the identity plugin, health-checks it, and stores the client.
func (r *Registry) loadIdentity(ctx context.Context, cfg config.PluginConfig, factory *pluginClientFactory, httpClient *http.Client) error {
	addr := ensureScheme(cfg.Addr)

	healthClient := factory.newPluginClient(httpClient, addr)
	healthMsg, err := checkHealth(ctx, healthClient)
	if err != nil {
		return err
	}

	if err := CheckVersionFromHealthMessage(PluginIdentity, healthMsg); err != nil {
		slog.Warn("identity plugin version negotiation issue", "error", err)
	}

	identityClient := factory.newIdentityClient(httpClient, addr)
	r.identity = &identityPlugin{client: identityClient}

	slog.Info("identity plugin loaded", "addr", cfg.Addr)
	return nil
}

// IdentityEnabled returns true if the identity plugin is loaded and healthy.
func (r *Registry) IdentityEnabled() bool {
	return r.identity != nil
}

// GetIdentityCapabilities delegates to the identity plugin's GetCapabilities RPC.
func (r *Registry) GetIdentityCapabilities(ctx context.Context) (*identityv1.GetCapabilitiesResponse, error) {
	if r.identity == nil {
		return nil, fmt.Errorf("identity plugin not loaded")
	}
	resp, err := r.identity.client.GetCapabilities(ctx, connect.NewRequest(&identityv1.GetCapabilitiesRequest{}))
	if err != nil {
		return nil, fmt.Errorf("get identity capabilities: %w", err)
	}
	return resp.Msg, nil
}

// ListIdentityUsers delegates to the identity plugin's ListUsers RPC.
func (r *Registry) ListIdentityUsers(ctx context.Context, search string, limit, offset int32) (*identityv1.ListUsersResponse, error) {
	if r.identity == nil {
		return nil, fmt.Errorf("identity plugin not loaded")
	}
	resp, err := r.identity.client.ListUsers(ctx, connect.NewRequest(&identityv1.ListUsersRequest{
		Search: search,
		Limit:  limit,
		Offset: offset,
	}))
	if err != nil {
		return nil, fmt.Errorf("list identity users: %w", err)
	}
	return resp.Msg, nil
}

// GetIdentityUser delegates to the identity plugin's GetUser RPC.
func (r *Registry) GetIdentityUser(ctx context.Context, userID string) (*identityv1.GetUserResponse, error) {
	if r.identity == nil {
		return nil, fmt.Errorf("identity plugin not loaded")
	}
	resp, err := r.identity.client.GetUser(ctx, connect.NewRequest(&identityv1.GetUserRequest{
		UserId: userID,
	}))
	if err != nil {
		return nil, fmt.Errorf("get identity user: %w", err)
	}
	return resp.Msg, nil
}

// SearchIdentityUsers delegates to the identity plugin's SearchUsers RPC.
func (r *Registry) SearchIdentityUsers(ctx context.Context, query string, limit int32) (*identityv1.SearchUsersResponse, error) {
	if r.identity == nil {
		return nil, fmt.Errorf("identity plugin not loaded")
	}
	resp, err := r.identity.client.SearchUsers(ctx, connect.NewRequest(&identityv1.SearchUsersRequest{
		Query: query,
		Limit: limit,
	}))
	if err != nil {
		return nil, fmt.Errorf("search identity users: %w", err)
	}
	return resp.Msg, nil
}

// ListIdentityGroups delegates to the identity plugin's ListIdentityGroups RPC.
func (r *Registry) ListIdentityGroups(ctx context.Context) (*identityv1.ListIdentityGroupsResponse, error) {
	if r.identity == nil {
		return nil, fmt.Errorf("identity plugin not loaded")
	}
	resp, err := r.identity.client.ListIdentityGroups(ctx, connect.NewRequest(&identityv1.ListIdentityGroupsRequest{}))
	if err != nil {
		return nil, fmt.Errorf("list identity groups: %w", err)
	}
	return resp.Msg, nil
}

// ensureScheme adds http:// if the address has no scheme.
func ensureScheme(addr string) string {
	if addr != "" && !strings.HasPrefix(addr, "http://") && !strings.HasPrefix(addr, "https://") {
		return "http://" + addr
	}
	return addr
}
