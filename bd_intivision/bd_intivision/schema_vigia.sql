

CREATE TABLE plans (
    code            text     PRIMARY KEY,
    name            text     NOT NULL,
    price_pen_cents integer  NOT NULL CHECK (price_pen_cents >= 0),
    billing_period  text     NOT NULL DEFAULT 'month' CHECK (billing_period IN ('month', 'year')),
    live_view       boolean  NOT NULL DEFAULT true,
    ai_detection    boolean  NOT NULL DEFAULT false,
    realtime_alerts boolean  NOT NULL DEFAULT false,
    highlight_clips boolean  NOT NULL DEFAULT false,
    event_history   boolean  NOT NULL DEFAULT false,
    storage_gb      smallint NOT NULL DEFAULT 0,
    max_cameras     smallint,                 -- NULL = sin definir
    is_active       boolean  NOT NULL DEFAULT true
);
INSERT INTO plans (code, name, price_pen_cents,
                   ai_detection, realtime_alerts, highlight_clips, event_history, storage_gb)
VALUES ('base',    'Plan Base',    1490,  false, false, false, false, 0),
       ('premium', 'Plan Premium', 10990, true,  true,  true,  true,  50);

CREATE TABLE districts (
    ubigeo     char(6) PRIMARY KEY,
    department text    NOT NULL,
    province   text    NOT NULL,
    name       text    NOT NULL
);


CREATE TABLE communities (
    id              uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text         NOT NULL,
    slug            text         NOT NULL UNIQUE,
    district_ubigeo char(6)      NOT NULL REFERENCES districts (ubigeo),
    address_ref     text,
    center_lat      numeric(9,6) CHECK (center_lat BETWEEN -90  AND 90),
    center_lng      numeric(9,6) CHECK (center_lng BETWEEN -180 AND 180),
    settings        jsonb        NOT NULL DEFAULT '{}'::jsonb,
    media_key_ref   text,                      
    is_active       boolean      NOT NULL DEFAULT true,
    created_at      timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ix_communities_district ON communities (district_ubigeo);

CREATE TABLE users (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id    uuid        NOT NULL REFERENCES communities (id),
    role            text        NOT NULL DEFAULT 'resident'
                                CHECK (role IN ('resident', 'community_admin')),
    full_name       text        NOT NULL,
    email           text,
    phone           text,
    password_hash   text        NOT NULL,       -- argon2 o bcrypt, nunca texto plano
    status          text        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'suspended', 'canceled')),
    failed_attempts smallint    NOT NULL DEFAULT 0,
    locked_until    timestamptz,
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (email IS NOT NULL OR phone IS NOT NULL),
    CONSTRAINT ux_users_id_community UNIQUE (id, community_id)   -- habilita las FK compuestas
);
CREATE UNIQUE INDEX ux_users_email ON users (lower(email)) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX ux_users_phone ON users (phone)        WHERE phone IS NOT NULL;
CREATE INDEX ix_users_community_role ON users (community_id, role);


CREATE TABLE subscriptions (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    plan_code            text        NOT NULL REFERENCES plans (code),
    status               text        NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('trialing', 'active', 'past_due', 'canceled')),
    current_period_start timestamptz,
    current_period_end   timestamptz,
    cancel_at_period_end boolean     NOT NULL DEFAULT false,
    provider             text,
    provider_ref         text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    canceled_at          timestamptz
);
CREATE UNIQUE INDEX ux_one_live_subscription
    ON subscriptions (user_id) WHERE status IN ('trialing', 'active', 'past_due');

-- Celulares donde el usuario tiene la app (token de notificaciones push).
CREATE TABLE devices (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    platform     text        NOT NULL CHECK (platform IN ('ios', 'android')),
    push_token   text        UNIQUE,
    app_version  text,
    last_seen_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_devices_user ON devices (user_id);

-- Sesiones de la app. Se guarda el hash del token de refresco, nunca el token.
CREATE TABLE sessions (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    device_id  uuid        REFERENCES devices (id) ON DELETE SET NULL,
    token_hash text        NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    ip         inet,
    user_agent text
);
CREATE INDEX ix_sessions_user ON sessions (user_id);


CREATE TABLE edge_devices (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    name         text        NOT NULL,
    token_hash   text        NOT NULL UNIQUE,    -- hash del token; nunca el token
    status       text        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'active', 'revoked')),
    version      text,
    last_seen_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (community_id, name),
    CONSTRAINT ux_edge_id_community UNIQUE (id, community_id)
);

CREATE TABLE cameras (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id      uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    edge_device_id    uuid,
    user_id           uuid,
    kind              text        NOT NULL DEFAULT 'private' CHECK (kind IN ('private', 'common')),
    name              text        NOT NULL,
    place             text,                     -- ej. "Puerta principal"
    source_type       text        NOT NULL DEFAULT 'rtsp'
                                  CHECK (source_type IN ('rtsp', 'browser', 'file')),
    stream_secret_enc bytea,
    restricted_zone   jsonb,                    -- polígono en píxeles a 640 px de ancho
    admin_can_view    boolean     NOT NULL DEFAULT true,
    is_active         boolean     NOT NULL DEFAULT true,
    last_seen_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (community_id, name),
    CONSTRAINT ux_cameras_id_community UNIQUE (id, community_id),
    CHECK ((kind = 'private' AND user_id IS NOT NULL) OR (kind = 'common' AND user_id IS NULL)),
    FOREIGN KEY (user_id, community_id)
        REFERENCES users (id, community_id) ON DELETE CASCADE,
    FOREIGN KEY (edge_device_id, community_id)
        REFERENCES edge_devices (id, community_id) ON DELETE SET NULL (edge_device_id)
);
CREATE INDEX ix_cameras_user ON cameras (user_id);


CREATE TABLE events (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    user_id      uuid,
    camera_id    uuid,
    source       text        NOT NULL DEFAULT 'edge'
                             CHECK (source IN ('edge', 'browser', 'video')),
    level        text        NOT NULL CHECK (level IN ('BAJO', 'MEDIO', 'ALTO')),
    score        smallint    NOT NULL,
    people_count smallint    NOT NULL DEFAULT 0,
    status       text        NOT NULL DEFAULT 'sin verificar'
                             CHECK (status IN ('sin verificar', 'verificado', 'falsa alarma')),
    video_time_s numeric(10,2),
    occurred_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ux_events_id_community UNIQUE (id, community_id),
    FOREIGN KEY (user_id, community_id)
        REFERENCES users (id, community_id) ON DELETE CASCADE,
    FOREIGN KEY (camera_id, community_id)
        REFERENCES cameras (id, community_id) ON DELETE SET NULL (camera_id)
);
CREATE INDEX ix_events_community_time ON events (community_id, occurred_at DESC);
CREATE INDEX ix_events_user_time      ON events (user_id, occurred_at DESC);
CREATE INDEX ix_events_camera_time    ON events (camera_id, occurred_at DESC);

CREATE TABLE media_files (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id uuid        NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    user_id      uuid,
    camera_id    uuid,
    event_id     bigint,
    kind         text        NOT NULL CHECK (kind IN ('image', 'clip')),
    is_highlight boolean     NOT NULL DEFAULT false,   -- "clip destacado"
    storage_key  text        NOT NULL UNIQUE,
    size_bytes   bigint      NOT NULL CHECK (size_bytes >= 0),
    duration_s   numeric(8,2),
    sha256       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    FOREIGN KEY (user_id, community_id)
        REFERENCES users (id, community_id) ON DELETE CASCADE,
    FOREIGN KEY (camera_id, community_id)
        REFERENCES cameras (id, community_id) ON DELETE SET NULL (camera_id),
    FOREIGN KEY (event_id, community_id)
        REFERENCES events (id, community_id) ON DELETE CASCADE
);
CREATE INDEX ix_media_user_time      ON media_files (user_id, created_at DESC);
CREATE INDEX ix_media_community_time ON media_files (community_id, created_at DESC);
CREATE INDEX ix_media_event          ON media_files (event_id);


CREATE TABLE signals (
    code        text PRIMARY KEY,
    description text NOT NULL
);
INSERT INTO signals (code, description) VALUES
    ('person_present',     'Persona detectada'),
    ('in_restricted_zone', 'En zona restringida'),
    ('sensitive_hour',     'Horario sensible'),
    ('loitering',          'Merodeo'),
    ('group',              'Grupo de personas'),
    ('climbing',           'Trepando'),
    ('fallen',             'Persona en el suelo'),
    ('hands_up',           'Posible asalto (manos arriba)');

CREATE TABLE event_signals (
    event_id    bigint NOT NULL REFERENCES events (id)    ON DELETE CASCADE,
    signal_code text   NOT NULL REFERENCES signals (code),
    detail      text,
    PRIMARY KEY (event_id, signal_code)
);
CREATE INDEX ix_event_signals_code ON event_signals (signal_code);


CREATE TABLE notification_channels (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid        REFERENCES users (id)       ON DELETE CASCADE,
    community_id    uuid        REFERENCES communities (id) ON DELETE CASCADE,
    kind            text        NOT NULL CHECK (kind IN ('push', 'telegram')),
    device_id       uuid        REFERENCES devices (id) ON DELETE CASCADE,
    chat_id         text,                      -- Telegram: ID del chat (persona o grupo)
    token_encrypted bytea,                     -- opcional: bot propio del cliente, cifrado
    link_code_hash  text,                      -- hash del código de vinculación
    link_expires_at timestamptz,               -- vencimiento del código
    min_level       text        NOT NULL DEFAULT 'MEDIO' CHECK (min_level IN ('MEDIO', 'ALTO')),
    is_active       boolean     NOT NULL DEFAULT true,
    verified_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((user_id IS NOT NULL)::int + (community_id IS NOT NULL)::int = 1),
    CHECK (
        (kind = 'push'     AND user_id IS NOT NULL AND device_id IS NOT NULL) OR
        (kind = 'telegram' AND (chat_id IS NOT NULL OR link_code_hash IS NOT NULL))
    )
);
CREATE INDEX ix_channels_user      ON notification_channels (user_id);
CREATE INDEX ix_channels_community ON notification_channels (community_id);
CREATE UNIQUE INDEX ux_channels_telegram_chat
    ON notification_channels (chat_id) WHERE kind = 'telegram' AND chat_id IS NOT NULL;
CREATE UNIQUE INDEX ux_channels_link_code
    ON notification_channels (link_code_hash) WHERE link_code_hash IS NOT NULL;

-- Cada intento de envío. Si falla, el evento ya está guardado.
CREATE TABLE alert_deliveries (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   bigint      NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    channel_id uuid        NOT NULL REFERENCES notification_channels (id) ON DELETE CASCADE,
    status     text        NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'failed')),
    attempts   smallint    NOT NULL DEFAULT 0,
    last_error text,
    sent_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_deliveries_open
    ON alert_deliveries (status, created_at) WHERE status <> 'sent';

CREATE TABLE staff_users (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text        NOT NULL,
    full_name     text        NOT NULL,
    password_hash text        NOT NULL,
    role          text        NOT NULL CHECK (role IN ('support', 'billing', 'admin')),
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_staff_email ON staff_users (lower(email));

CREATE TABLE support_access_grants (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid        NOT NULL REFERENCES users (id)       ON DELETE CASCADE,
    staff_id   uuid        NOT NULL REFERENCES staff_users (id) ON DELETE CASCADE,
    scope      text        NOT NULL DEFAULT 'metadata' CHECK (scope IN ('metadata', 'media')),
    reason     text        NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_support_grants_user ON support_access_grants (user_id, expires_at DESC);

CREATE TABLE audit_log (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_type      text        NOT NULL CHECK (actor_type IN ('user', 'staff', 'edge', 'system')),
    actor_id        uuid,
    community_id    uuid        REFERENCES communities (id) ON DELETE SET NULL,
    subject_user_id uuid        REFERENCES users (id)       ON DELETE SET NULL,
    action          text        NOT NULL,
    entity          text,
    entity_id       text,
    ip              inet,
    meta            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_community_time ON audit_log (community_id, created_at DESC);
CREATE INDEX ix_audit_subject_time   ON audit_log (subject_user_id, created_at DESC);
CREATE INDEX ix_audit_actor_time     ON audit_log (actor_type, actor_id, created_at DESC);



CREATE VIEW v_user_storage AS
SELECT u.id AS user_id,
       p.code AS plan_code,
       coalesce(sum(m.size_bytes), 0)             AS used_bytes,
       p.storage_gb::bigint * 1024 * 1024 * 1024  AS quota_bytes
FROM users u
JOIN subscriptions s ON s.user_id = u.id
                    AND s.status IN ('trialing', 'active', 'past_due')
JOIN plans p ON p.code = s.plan_code
LEFT JOIN media_files m ON m.user_id = u.id
GROUP BY u.id, p.code, p.storage_gb;


CREATE VIEW v_community_day_stats AS
SELECT e.community_id,
       (e.occurred_at AT TIME ZONE 'America/Lima')::date AS day,
       e.level,
       count(*) AS events
FROM events e
GROUP BY e.community_id, 2, e.level;

CREATE VIEW v_district_hour_stats AS
SELECT c.district_ubigeo,
       extract(isodow FROM e.occurred_at AT TIME ZONE 'America/Lima')::int AS weekday,
       extract(hour   FROM e.occurred_at AT TIME ZONE 'America/Lima')::int AS hour,
       e.level,
       count(*)                      AS events,
       count(DISTINCT e.community_id) AS communities
FROM events e
JOIN communities c ON c.id = e.community_id
GROUP BY c.district_ubigeo, 2, 3, e.level
HAVING count(DISTINCT e.community_id) >= 3;

CREATE VIEW v_district_signal_stats AS
SELECT c.district_ubigeo,
       es.signal_code,
       count(*)                      AS occurrences,
       count(DISTINCT e.community_id) AS communities
FROM event_signals es
JOIN events e      ON e.id = es.event_id
JOIN communities c ON c.id = e.community_id
GROUP BY c.district_ubigeo, es.signal_code
HAVING count(DISTINCT e.community_id) >= 3;

CREATE VIEW v_community_map AS
SELECT c.id AS community_id,
       c.name,
       c.district_ubigeo,
       c.center_lat,
       c.center_lng,
       count(e.id) FILTER (WHERE e.level = 'ALTO')  AS high_30d,
       count(e.id) FILTER (WHERE e.level = 'MEDIO') AS medium_30d
FROM communities c
LEFT JOIN events e ON e.community_id = c.id
                  AND e.occurred_at >= now() - interval '30 days'
GROUP BY c.id;