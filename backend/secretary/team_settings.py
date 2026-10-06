"""Explicit protected Team configuration. Importing this module reads no environment.

The original Settings type is used only by secretary_settings, through an
init-only source subclass. The CLI never falls back to .env or process secrets.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re
import stat
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class RuntimeConfigurationError(ValueError):
    def __init__(self, code='team_runtime_config_invalid'):
        self.code = code
        super().__init__(code)


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('runtime_uuid_invalid')
    return value


def local_path(value):
    if not isinstance(value, (str, Path)) or not str(value) or str(value).startswith(('\\\\', '//')):
        raise ValueError('runtime_local_path_required')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('runtime_absolute_path_required')
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('runtime_reparse_path_forbidden')
    return path.resolve(strict=False)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class TokenEvidence(StrictModel):
    owner_id: StrictStr
    token_id: StrictStr
    token_sha256: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]

    @field_validator('owner_id', 'token_id')
    @classmethod
    def identifier(cls, value):
        if not re.fullmatch(r'[1-9][0-9]{0,127}', value):
            raise ValueError('runtime_identifier_invalid')
        return value


class RuntimeProjectBinding(StrictModel):
    project_id: StrictStr
    manual_view_id: StrictStr
    bot_user_id: StrictStr
    bucket_ids: dict[StrictStr, StrictStr]
    important_label_id: StrictStr
    urgent_label_id: StrictStr
    cancelled_label_id: StrictStr

    @model_validator(mode='after')
    def valid_binding(self):
        from secretary.infrastructure.vikunja import ProjectBinding
        ProjectBinding(**self.model_dump())
        return self


class TeamRuntimeSettings(StrictModel):
    schema_version: Literal[1] = 1
    deployment_id: StrictStr
    project_dir: Path
    secretary_database_path: Path
    team_database_path: Path
    billing_database_path: Path
    vikunja_database_path: Path
    control_database_path: Path
    local_secretary_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 8765
    gateway_port: Annotated[int, Field(strict=True, ge=1, le=65535)] = 8766
    public_origin: StrictStr | None = None
    max_bot_id: StrictStr | None = None
    max_bot_username: StrictStr | None = None
    local_owner_id: StrictStr | None = None
    publication_project_id: StrictStr | None = None
    max_bot_token: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    max_webhook_secret: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    team_auth_secret: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    publication_service_secret: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    polza_api_key: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    vikunja_token: SecretStr = Field(default_factory=lambda: SecretStr(''), repr=False)
    vikunja_base_url: StrictStr = 'http://127.0.0.1:3456/api/v2/'
    vikunja_credential_binding: TokenEvidence | None = None
    project_bindings: Annotated[tuple[RuntimeProjectBinding, ...], Field(max_length=10)] = ()
    media_allowlisted_hosts: Annotated[tuple[StrictStr, ...], Field(max_length=32)] = ()
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    voice_scratch_path: Path | None = None
    outbound_enabled: StrictBool = False
    restore_blocked: StrictBool = False
    subscription_reconcile_enabled: StrictBool = False
    approved_monthly_external_costs_rub: Decimal = Decimal('0')
    secretary_options: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator('schema_version', mode='before')
    @classmethod
    def schema(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('runtime_schema_invalid')
        return value

    @field_validator('deployment_id', 'local_owner_id')
    @classmethod
    def uuid_valid(cls, value):
        return _uuid(value) if value is not None else None

    @field_validator('project_dir', 'secretary_database_path', 'team_database_path',
        'billing_database_path', 'vikunja_database_path', 'control_database_path',
        'ffmpeg_path', 'ffprobe_path', 'voice_scratch_path', mode='before')
    @classmethod
    def paths(cls, value):
        return local_path(value) if value is not None else None

    @field_validator('project_bindings', 'media_allowlisted_hosts', mode='before')
    @classmethod
    def tuples(cls, value):
        if type(value) is list:
            return tuple(value)
        return value

    @field_validator('approved_monthly_external_costs_rub', mode='before')
    @classmethod
    def costs(cls, value):
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal, float)):
            raise ValueError('runtime_external_cost_invalid')
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= 3000 or result.as_tuple().exponent < -6:
            raise ValueError('runtime_external_cost_invalid')
        return result

    @field_validator('public_origin')
    @classmethod
    def origin(cls, value):
        if value is None:
            return value
        from secretary.interface.team_gateway import TeamGatewaySettings
        result = TeamGatewaySettings.https_origin(value)
        # Production proxy and MAX webhook are pinned to standard HTTPS443.
        from urllib.parse import urlsplit
        if urlsplit(result).port not in (None, 443):
            raise ValueError('runtime_https443_required')
        return result

    @field_validator('max_bot_id')
    @classmethod
    def bot_id(cls, value):
        if value is not None and (not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 2**63 - 1):
            raise ValueError('runtime_bot_id_invalid')
        return value

    @field_validator('media_allowlisted_hosts')
    @classmethod
    def hosts(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', v)
                or v.endswith('.localhost') for v in values):
            raise ValueError('runtime_media_hosts_invalid')
        return values

    @field_validator('vikunja_base_url')
    @classmethod
    def native_origin(cls, value):
        import ipaddress
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or not parsed.hostname or not ipaddress.ip_address(parsed.hostname).is_loopback
                or not parsed.port or parsed.path.rstrip('/') != '/api/v2'
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError('runtime_vikunja_loopback_required')
        return value

    @model_validator(mode='after')
    def coherent(self):
        databases = tuple(getattr(self, name) for name in ('secretary_database_path', 'team_database_path',
            'billing_database_path', 'vikunja_database_path', 'control_database_path'))
        if len(set(databases)) != 5 or self.gateway_port == self.local_secretary_port:
            raise ValueError('runtime_storage_or_port_collision')
        for path in (*databases, self.voice_scratch_path):
            if path is not None and (path == self.project_dir or not path.is_relative_to(self.project_dir)):
                raise ValueError('runtime_path_outside_project')
        projects = tuple(item.project_id for item in self.project_bindings)
        if len(projects) != len(set(projects)) or self.publication_project_id is not None and self.publication_project_id not in projects:
            raise ValueError('runtime_project_binding_invalid')
        if self.subscription_reconcile_enabled and not self.outbound_enabled:
            raise ValueError('runtime_subscription_outbound_required')
        # Validate Secretary options with no dotenv/process-env/file-secret sources.
        secretary_settings(self)
        return self

    @property
    def outbound_allowed(self):
        return self.outbound_enabled and not self.restore_blocked

    @property
    def publication_configured(self):
        return bool(self.local_owner_id and self.publication_project_id and self.publication_service_secret.get_secret_value())

    def configuration_code(self):
        if not (self.public_origin and self.max_bot_id and self.max_bot_token.get_secret_value()
                and self.max_webhook_secret.get_secret_value()):
            return 'max_configuration_required'
        if len(self.team_auth_secret.get_secret_value().encode('utf-8')) < 32:
            return 'team_auth_configuration_required'
        if not self.local_owner_id or not self.project_bindings or not self.vikunja_token.get_secret_value():
            return 'team_binding_configuration_required'
        return None


def secretary_settings(config: TeamRuntimeSettings):
    from secretary.settings import Settings

    class ExplicitSettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
            return (init_settings,)

    protected = {'project_dir', 'data_dir', 'polza_api_key', 'approved_monthly_external_costs_rub'}
    if (any(key not in Settings.model_fields or key in protected for key in config.secretary_options)
            or any(key in config.secretary_options for key in ('polza_base_url',))):
        raise RuntimeConfigurationError()
    try:
        options = dict(config.secretary_options)
        if 'cloud_enabled' in options and type(options['cloud_enabled']) is not bool:
            raise ValueError('runtime_cloud_flag_invalid')
        options['cloud_enabled'] = bool(config.outbound_allowed and options.get('cloud_enabled', True))
        return ExplicitSettings(_env_file=None, **options, project_dir=config.project_dir,
            data_dir=config.secretary_database_path.parent, polza_api_key=config.polza_api_key,
            approved_monthly_external_costs_rub=float(config.approved_monthly_external_costs_rub))
    except (ValueError, TypeError):
        raise RuntimeConfigurationError() from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_configuration_field')
        result[key] = value
    return result


def load_team_settings(config_path: Path) -> TeamRuntimeSettings:
    try:
        path = local_path(config_path)
        if path.stat().st_size > 128 * 1024 or not path.is_file():
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_configuration')))
        return TeamRuntimeSettings.model_validate(value)
    except (OSError, ValueError, TypeError):
        raise RuntimeConfigurationError() from None
