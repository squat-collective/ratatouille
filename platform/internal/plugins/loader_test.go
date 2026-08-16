package plugins

import (
	"context"
	"errors"
	"net/http"
	"testing"

	connect "connectrpc.com/connect"
	authv1 "github.com/rat-data/rat/platform/gen/auth/v1"
	"github.com/rat-data/rat/platform/gen/auth/v1/authv1connect"
	cloudv1 "github.com/rat-data/rat/platform/gen/cloud/v1"
	"github.com/rat-data/rat/platform/gen/cloud/v1/cloudv1connect"
	commonv1 "github.com/rat-data/rat/platform/gen/common/v1"
	executorv1 "github.com/rat-data/rat/platform/gen/executor/v1"
	permissionv1 "github.com/rat-data/rat/platform/gen/permission/v1"
	"github.com/rat-data/rat/platform/gen/permission/v1/permissionv1connect"
	"github.com/rat-data/rat/platform/gen/executor/v1/executorv1connect"
	pluginv1 "github.com/rat-data/rat/platform/gen/plugin/v1"
	"github.com/rat-data/rat/platform/gen/plugin/v1/pluginv1connect"
	"github.com/rat-data/rat/platform/internal/config"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Mock plugin client ---

type mockPluginClient struct {
	healthFunc func(ctx context.Context, req *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error)
}

func (m *mockPluginClient) HealthCheck(ctx context.Context, req *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
	if m.healthFunc != nil {
		return m.healthFunc(ctx, req)
	}
	return connect.NewResponse(&pluginv1.HealthCheckResponse{
		Status: pluginv1.Status_STATUS_SERVING,
	}), nil
}

// --- Mock auth client ---

type mockAuthClient struct {
	authenticateFunc func(ctx context.Context, req *connect.Request[authv1.AuthenticateRequest]) (*connect.Response[authv1.AuthenticateResponse], error)
	authorizeFunc    func(ctx context.Context, req *connect.Request[authv1.AuthorizeRequest]) (*connect.Response[authv1.AuthorizeResponse], error)
}

func (m *mockAuthClient) Authenticate(ctx context.Context, req *connect.Request[authv1.AuthenticateRequest]) (*connect.Response[authv1.AuthenticateResponse], error) {
	if m.authenticateFunc != nil {
		return m.authenticateFunc(ctx, req)
	}
	return connect.NewResponse(&authv1.AuthenticateResponse{
		Authenticated: true,
		User: &authv1.UserIdentity{
			UserId:      "u-123",
			Email:       "remy@rat.dev",
			DisplayName: "Remy",
		},
	}), nil
}

func (m *mockAuthClient) Authorize(ctx context.Context, req *connect.Request[authv1.AuthorizeRequest]) (*connect.Response[authv1.AuthorizeResponse], error) {
	if m.authorizeFunc != nil {
		return m.authorizeFunc(ctx, req)
	}
	return connect.NewResponse(&authv1.AuthorizeResponse{Allowed: true}), nil
}

// --- Mock executor client ---

type mockExecutorClient struct{}

func (m *mockExecutorClient) Submit(_ context.Context, _ *connect.Request[executorv1.SubmitRequest]) (*connect.Response[executorv1.SubmitResponse], error) {
	return connect.NewResponse(&executorv1.SubmitResponse{}), nil
}

func (m *mockExecutorClient) GetRunStatus(_ context.Context, _ *connect.Request[commonv1.GetRunStatusRequest]) (*connect.Response[commonv1.GetRunStatusResponse], error) {
	return connect.NewResponse(&commonv1.GetRunStatusResponse{}), nil
}

func (m *mockExecutorClient) StreamLogs(_ context.Context, _ *connect.Request[commonv1.StreamLogsRequest]) (*connect.ServerStreamForClient[commonv1.LogEntry], error) {
	return nil, connect.NewError(connect.CodeUnimplemented, errors.New("not implemented"))
}

func (m *mockExecutorClient) Cancel(_ context.Context, _ *connect.Request[commonv1.CancelRunRequest]) (*connect.Response[commonv1.CancelRunResponse], error) {
	return connect.NewResponse(&commonv1.CancelRunResponse{Cancelled: true}), nil
}

// --- Mock cloud client ---

type mockCloudClient struct {
	getCredentialsFunc func(ctx context.Context, req *connect.Request[cloudv1.GetCredentialsRequest]) (*connect.Response[cloudv1.GetCredentialsResponse], error)
}

func (m *mockCloudClient) GetCredentials(ctx context.Context, req *connect.Request[cloudv1.GetCredentialsRequest]) (*connect.Response[cloudv1.GetCredentialsResponse], error) {
	if m.getCredentialsFunc != nil {
		return m.getCredentialsFunc(ctx, req)
	}
	return connect.NewResponse(&cloudv1.GetCredentialsResponse{
		AccessKey:    "AKIA-TEST",
		SecretKey:    "SECRET-TEST",
		SessionToken: "TOKEN-TEST",
		Region:       "us-east-1",
		Bucket:       "rat",
		ExpiresAt:    1700000000,
	}), nil
}

// --- Mock permission client ---

type mockPermissionClient struct {
	permissionv1connect.UnimplementedPermissionServiceHandler
	listGroupsFunc func(context.Context, *connect.Request[permissionv1.ListGroupsRequest]) (*connect.Response[permissionv1.ListGroupsResponse], error)
}

func (m *mockPermissionClient) ListGroups(ctx context.Context, req *connect.Request[permissionv1.ListGroupsRequest]) (*connect.Response[permissionv1.ListGroupsResponse], error) {
	if m.listGroupsFunc != nil {
		return m.listGroupsFunc(ctx, req)
	}
	return connect.NewResponse(&permissionv1.ListGroupsResponse{}), nil
}

// --- Test factory ---

func mockFactory(plugin *mockPluginClient, auth *mockAuthClient) *pluginClientFactory {
	return mockFactoryFull(plugin, auth, &mockExecutorClient{}, &mockCloudClient{})
}

func mockFactoryWithExecutor(plugin *mockPluginClient, auth *mockAuthClient, exec executorv1connect.ExecutorServiceClient) *pluginClientFactory {
	return mockFactoryFull(plugin, auth, exec, &mockCloudClient{})
}

func mockFactoryFull(plugin *mockPluginClient, auth *mockAuthClient, exec executorv1connect.ExecutorServiceClient, cloud cloudv1connect.CloudServiceClient) *pluginClientFactory {
	return mockFactoryWithAll(plugin, auth, exec, cloud, &mockPermissionClient{})
}

func mockFactoryWithAll(plugin *mockPluginClient, auth *mockAuthClient, exec executorv1connect.ExecutorServiceClient, cloud cloudv1connect.CloudServiceClient, perm permissionv1connect.PermissionServiceClient) *pluginClientFactory {
	return &pluginClientFactory{
		newPluginClient: func(_ connect.HTTPClient, _ string, _ ...connect.ClientOption) pluginv1connect.PluginServiceClient {
			return plugin
		},
		newAuthClient: func(_ connect.HTTPClient, _ string, _ ...connect.ClientOption) authv1connect.AuthServiceClient {
			return auth
		},
		newExecutorClient: func(_ connect.HTTPClient, _ string, _ ...connect.ClientOption) executorv1connect.ExecutorServiceClient {
			return exec
		},
		newCloudClient: func(_ connect.HTTPClient, _ string, _ ...connect.ClientOption) cloudv1connect.CloudServiceClient {
			return cloud
		},
		newPermissionClient: func(_ connect.HTTPClient, _ string, _ ...connect.ClientOption) permissionv1connect.PermissionServiceClient {
			return perm
		},
	}
}

// --- Tests ---

func TestLoad_NoPlugins_EmptyRegistry(t *testing.T) {
	cfg := config.DefaultConfig()
	reg, err := loadWithFactory(context.Background(), cfg, defaultFactory(), http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.AuthEnabled())
}

func TestLoad_AuthPlugin_Healthy_Enabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"auth": {Addr: "auth:50060"},
		},
	}

	plugin := &mockPluginClient{}
	auth := &mockAuthClient{}
	factory := mockFactory(plugin, auth)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.True(t, reg.AuthEnabled())
}

func TestLoad_AuthPlugin_Unhealthy_Disabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"auth": {Addr: "auth:50060"},
		},
	}

	plugin := &mockPluginClient{
		healthFunc: func(_ context.Context, _ *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
			return nil, connect.NewError(connect.CodeUnavailable, errors.New("connection refused"))
		},
	}
	auth := &mockAuthClient{}
	factory := mockFactory(plugin, auth)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.AuthEnabled(), "unhealthy auth plugin should be disabled")
}

func TestLoad_AuthPlugin_NotServing_Disabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"auth": {Addr: "auth:50060"},
		},
	}

	plugin := &mockPluginClient{
		healthFunc: func(_ context.Context, _ *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
			return connect.NewResponse(&pluginv1.HealthCheckResponse{
				Status:  pluginv1.Status_STATUS_NOT_SERVING,
				Message: "shutting down",
			}), nil
		},
	}
	auth := &mockAuthClient{}
	factory := mockFactory(plugin, auth)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.AuthEnabled(), "not-serving auth plugin should be disabled")
}

func TestLoad_UnknownPlugin_Skipped(t *testing.T) {
	cfg := &config.Config{
		Edition: "community",
		Plugins: map[string]config.PluginConfig{
			"banana": {Addr: "banana:9999"},
		},
	}

	reg, err := loadWithFactory(context.Background(), cfg, defaultFactory(), http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.AuthEnabled())
}

func TestFeatures_NoPlugins_CommunityDefaults(t *testing.T) {
	reg := &Registry{edition: "community"}
	features := reg.Features()

	assert.Equal(t, "community", features.Edition)
	assert.False(t, features.Namespaces)
	assert.False(t, features.MultiUser)
	assert.False(t, features.Plugins["auth"].Enabled)
	assert.True(t, features.Plugins["executor"].Enabled)
	assert.Equal(t, "warmpool", features.Plugins["executor"].Type)
}

func TestFeatures_AuthEnabled_ReflectsInFeatures(t *testing.T) {
	reg := &Registry{
		edition: "pro",
		auth:    &authPlugin{client: &mockAuthClient{}},
	}
	features := reg.Features()

	assert.Equal(t, "pro", features.Edition)
	assert.True(t, features.Namespaces)
	assert.True(t, features.MultiUser)
	assert.True(t, features.Plugins["auth"].Enabled)
}

func TestAuthenticate_WithPlugin_DelegatesToClient(t *testing.T) {
	var captured string
	mock := &mockAuthClient{
		authenticateFunc: func(_ context.Context, req *connect.Request[authv1.AuthenticateRequest]) (*connect.Response[authv1.AuthenticateResponse], error) {
			captured = req.Msg.Token
			return connect.NewResponse(&authv1.AuthenticateResponse{
				Authenticated: true,
				User: &authv1.UserIdentity{
					UserId: "u-456",
					Email:  "linguini@rat.dev",
				},
			}), nil
		},
	}

	reg := &Registry{
		edition: "pro",
		auth:    &authPlugin{client: mock},
	}

	resp, err := reg.Authenticate(context.Background(), "my-token")
	require.NoError(t, err)

	assert.Equal(t, "my-token", captured)
	assert.True(t, resp.Authenticated)
	assert.Equal(t, "u-456", resp.User.UserId)
}

func TestAuthenticate_NoPlugin_ReturnsError(t *testing.T) {
	reg := &Registry{edition: "community"}

	_, err := reg.Authenticate(context.Background(), "token")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not loaded")
}

func TestEnsureScheme_AddsHTTP(t *testing.T) {
	assert.Equal(t, "http://auth:50060", ensureScheme("auth:50060"))
}

func TestEnsureScheme_PreservesExisting(t *testing.T) {
	assert.Equal(t, "https://auth:50060", ensureScheme("https://auth:50060"))
	assert.Equal(t, "http://auth:50060", ensureScheme("http://auth:50060"))
}

// --- Executor plugin tests ---

func TestLoad_ExecutorPlugin_Healthy_Enabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"executor": {Addr: "executor:50070"},
		},
	}

	plugin := &mockPluginClient{}
	auth := &mockAuthClient{}
	exec := &mockExecutorClient{}
	factory := mockFactoryWithExecutor(plugin, auth, exec)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.True(t, reg.ExecutorEnabled())
	assert.Equal(t, "http://executor:50070", reg.GetExecutorAddr())
}

func TestLoad_ExecutorPlugin_Unhealthy_Disabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"executor": {Addr: "executor:50070"},
		},
	}

	plugin := &mockPluginClient{
		healthFunc: func(_ context.Context, _ *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
			return nil, connect.NewError(connect.CodeUnavailable, errors.New("connection refused"))
		},
	}
	auth := &mockAuthClient{}
	exec := &mockExecutorClient{}
	factory := mockFactoryWithExecutor(plugin, auth, exec)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.ExecutorEnabled(), "unhealthy executor plugin should be disabled")
}

func TestFeatures_ExecutorPlugin_ReportsContainer(t *testing.T) {
	reg := &Registry{
		edition:  "pro",
		executor: &executorPlugin{client: &mockExecutorClient{}, addr: "http://executor:50070"},
	}
	features := reg.Features()

	assert.Equal(t, "container", features.Plugins["executor"].Type)
	assert.True(t, features.Plugins["executor"].Enabled)
}

func TestFeatures_NoExecutorPlugin_ReportsWarmpool(t *testing.T) {
	reg := &Registry{edition: "community"}
	features := reg.Features()

	assert.Equal(t, "warmpool", features.Plugins["executor"].Type)
	assert.True(t, features.Plugins["executor"].Enabled)
}

// --- Cloud plugin tests ---

func TestLoad_CloudPlugin_Healthy_Enabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"cloud": {Addr: "cloud-aws:50090"},
		},
	}

	plugin := &mockPluginClient{}
	auth := &mockAuthClient{}
	cloud := &mockCloudClient{}
	factory := mockFactoryFull(plugin, auth, &mockExecutorClient{}, cloud)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.True(t, reg.CloudEnabled())
}

func TestLoad_CloudPlugin_Unhealthy_Disabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"cloud": {Addr: "cloud-aws:50090"},
		},
	}

	plugin := &mockPluginClient{
		healthFunc: func(_ context.Context, _ *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
			return nil, connect.NewError(connect.CodeUnavailable, errors.New("connection refused"))
		},
	}
	auth := &mockAuthClient{}
	cloud := &mockCloudClient{}
	factory := mockFactoryFull(plugin, auth, &mockExecutorClient{}, cloud)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.CloudEnabled(), "unhealthy cloud plugin should be disabled")
}

func TestGetCredentials_WithPlugin_DelegatesToClient(t *testing.T) {
	var capturedUserID, capturedNamespace string
	mock := &mockCloudClient{
		getCredentialsFunc: func(_ context.Context, req *connect.Request[cloudv1.GetCredentialsRequest]) (*connect.Response[cloudv1.GetCredentialsResponse], error) {
			capturedUserID = req.Msg.UserId
			capturedNamespace = req.Msg.Namespace
			return connect.NewResponse(&cloudv1.GetCredentialsResponse{
				AccessKey:    "AKIA-SCOPED",
				SecretKey:    "SECRET-SCOPED",
				SessionToken: "TOKEN-SCOPED",
				Region:       "eu-west-1",
				Bucket:       "my-bucket",
				ExpiresAt:    1700001000,
			}), nil
		},
	}

	reg := &Registry{
		edition: "pro",
		cloud:   &cloudPlugin{client: mock},
	}

	resp, err := reg.GetCredentials(context.Background(), "u-789", "ecommerce")
	require.NoError(t, err)

	assert.Equal(t, "u-789", capturedUserID)
	assert.Equal(t, "ecommerce", capturedNamespace)
	assert.Equal(t, "AKIA-SCOPED", resp.AccessKey)
	assert.Equal(t, "TOKEN-SCOPED", resp.SessionToken)
	assert.Equal(t, "eu-west-1", resp.Region)
}

func TestGetCredentials_NoPlugin_ReturnsError(t *testing.T) {
	reg := &Registry{edition: "community"}

	_, err := reg.GetCredentials(context.Background(), "u-123", "default")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not loaded")
}

func TestFeatures_CloudPlugin_ReflectsInFeatures(t *testing.T) {
	reg := &Registry{
		edition: "pro",
		cloud:   &cloudPlugin{client: &mockCloudClient{}},
	}
	features := reg.Features()

	assert.True(t, features.Plugins["cloud"].Enabled)
}

func TestFeatures_NoCloudPlugin_DisabledInFeatures(t *testing.T) {
	reg := &Registry{edition: "community"}
	features := reg.Features()

	assert.False(t, features.Plugins["cloud"].Enabled)
}

// --- Permission plugin tests ---

func TestLoad_PermissionPlugin_Healthy_Enabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"permission": {Addr: "acl:50080"},
		},
	}

	plugin := &mockPluginClient{}
	auth := &mockAuthClient{}
	perm := &mockPermissionClient{}
	factory := mockFactoryWithAll(plugin, auth, &mockExecutorClient{}, &mockCloudClient{}, perm)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.True(t, reg.PermissionEnabled())
}

func TestLoad_PermissionPlugin_Unhealthy_Disabled(t *testing.T) {
	cfg := &config.Config{
		Edition: "pro",
		Plugins: map[string]config.PluginConfig{
			"permission": {Addr: "acl:50080"},
		},
	}

	plugin := &mockPluginClient{
		healthFunc: func(_ context.Context, _ *connect.Request[pluginv1.HealthCheckRequest]) (*connect.Response[pluginv1.HealthCheckResponse], error) {
			return nil, connect.NewError(connect.CodeUnavailable, errors.New("connection refused"))
		},
	}
	auth := &mockAuthClient{}
	perm := &mockPermissionClient{}
	factory := mockFactoryWithAll(plugin, auth, &mockExecutorClient{}, &mockCloudClient{}, perm)

	reg, err := loadWithFactory(context.Background(), cfg, factory, http.DefaultClient)
	require.NoError(t, err)

	assert.False(t, reg.PermissionEnabled(), "unhealthy permission plugin should be disabled")
}

func TestFeatures_PermissionPlugin_ReflectsInFeatures(t *testing.T) {
	reg := &Registry{
		edition:    "pro",
		permission: &permissionPlugin{client: &mockPermissionClient{}},
	}
	features := reg.Features()

	assert.True(t, features.Plugins["permission"].Enabled)
}

func TestFeatures_NoPermissionPlugin_DisabledInFeatures(t *testing.T) {
	reg := &Registry{edition: "community"}
	features := reg.Features()

	assert.False(t, features.Plugins["permission"].Enabled)
}

func TestListGroups_WithPlugin_DelegatesToClient(t *testing.T) {
	mock := &mockPermissionClient{
		listGroupsFunc: func(_ context.Context, _ *connect.Request[permissionv1.ListGroupsRequest]) (*connect.Response[permissionv1.ListGroupsResponse], error) {
			return connect.NewResponse(&permissionv1.ListGroupsResponse{
				Groups: []*permissionv1.GroupInfo{
					{GroupId: "grp-1", Name: "data-eng"},
				},
			}), nil
		},
	}

	reg := &Registry{
		edition:    "pro",
		permission: &permissionPlugin{client: mock},
	}

	resp, err := reg.ListGroups(context.Background())
	require.NoError(t, err)
	require.Len(t, resp.Groups, 1)
	assert.Equal(t, "data-eng", resp.Groups[0].Name)
}

func TestListGroups_NoPlugin_ReturnsError(t *testing.T) {
	reg := &Registry{edition: "community"}

	_, err := reg.ListGroups(context.Background())
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not loaded")
}
