CREATE TABLE guide_source (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_url text NOT NULL UNIQUE,
    title text NOT NULL,
    source_type text NOT NULL CHECK (source_type IN ('manual', 'web')),
    permission_ref text,
    CHECK (source_type <> 'web' OR permission_ref IS NOT NULL)
);

CREATE TABLE guide_version (
    id text PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES guide_source(id),
    title text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    processing_version text NOT NULL,
    entity_id text NOT NULL,
    guide_kind text NOT NULL CHECK (guide_kind = 'team'),
    game_from text NOT NULL,
    game_through text NOT NULL,
    game_from_key integer NOT NULL,
    game_through_key integer NOT NULL,
    applicability jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published', 'invalid')),
    CHECK (game_from_key <= game_through_key),
    CHECK (status <> 'published' OR (applicability->>'kind') IN ('known', 'unrestricted')),
    UNIQUE (source_id, content_hash, processing_version)
);

CREATE INDEX guide_version_scope ON guide_version
    (entity_id, guide_kind, game_from_key, game_through_key)
    WHERE status = 'published';

CREATE TABLE guide_chunk (
    id text COLLATE "C" PRIMARY KEY,
    version_id text NOT NULL REFERENCES guide_version(id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    section text NOT NULL,
    location text NOT NULL,
    body text NOT NULL,
    keyword_body text NOT NULL,
    terms text[] NOT NULL,
    UNIQUE (version_id, ordinal)
);

CREATE TABLE guide_embedding_config (
    id text PRIMARY KEY,
    model text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    UNIQUE (model, dimensions)
);

CREATE TABLE guide_index_checkpoint (
    version_id text NOT NULL REFERENCES guide_version(id),
    embedding_id text NOT NULL REFERENCES guide_embedding_config(id),
    expected_ids text[] NOT NULL CHECK (cardinality(expected_ids) > 0),
    ready boolean NOT NULL DEFAULT false,
    PRIMARY KEY (version_id, embedding_id)
);

CREATE TABLE guide_store_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT INTO guide_store_state DEFAULT VALUES;
