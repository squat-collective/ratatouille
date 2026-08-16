import type { ClientConfig } from "./config";
import { HealthResource } from "./resources/health";
import { NamespacesResource } from "./resources/namespaces";
import { PipelinesResource } from "./resources/pipelines";
import { QueryResource } from "./resources/query";
import { RunsResource } from "./resources/runs";
import { LandingResource } from "./resources/landing";
import { StorageResource } from "./resources/storage";
import { TablesResource } from "./resources/tables";
import { TriggersResource } from "./resources/triggers";
import { QualityResource } from "./resources/quality";
import { LineageResource } from "./resources/lineage";
import { RetentionResource } from "./resources/retention";
import { PermissionsResource } from "./resources/permissions";
import { IdentityResource } from "./resources/identity";
import { Transport } from "./transport";

/**
 * All fields optional -- apiUrl defaults to http://localhost:8080.
 * This is just `Partial<ClientConfig>` so callers only provide what they need.
 */
export type RatClientOptions = Partial<ClientConfig>;

export class RatClient {
  public readonly health: HealthResource;
  public readonly pipelines: PipelinesResource;
  public readonly runs: RunsResource;
  public readonly query: QueryResource;
  public readonly tables: TablesResource;
  public readonly storage: StorageResource;
  public readonly namespaces: NamespacesResource;
  public readonly landing: LandingResource;
  public readonly triggers: TriggersResource;
  public readonly quality: QualityResource;
  public readonly lineage: LineageResource;
  public readonly retention: RetentionResource;
  public readonly permissions: PermissionsResource;
  public readonly identity: IdentityResource;

  private readonly _config: ClientConfig;
  private readonly _transport: Transport;

  constructor(options: RatClientOptions = {}) {
    this._config = {
      apiUrl: options.apiUrl ?? "http://localhost:8080",
      timeout: options.timeout,
      maxRetries: options.maxRetries,
      headers: options.headers,
      onRequest: options.onRequest,
      onResponse: options.onResponse,
    };

    this._transport = new Transport(this._config);

    this.health = new HealthResource(this._transport);
    this.pipelines = new PipelinesResource(this._transport);
    this.runs = new RunsResource(this._transport);
    this.query = new QueryResource(this._transport);
    this.tables = new TablesResource(this._transport);
    this.storage = new StorageResource(this._transport);
    this.namespaces = new NamespacesResource(this._transport);
    this.landing = new LandingResource(this._transport);
    this.triggers = new TriggersResource(this._transport);
    this.quality = new QualityResource(this._transport);
    this.lineage = new LineageResource(this._transport);
    this.retention = new RetentionResource(this._transport);
    this.permissions = new PermissionsResource(this._transport);
    this.identity = new IdentityResource(this._transport);
  }

  get config(): ClientConfig {
    return this._config;
  }
}
