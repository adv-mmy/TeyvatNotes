ALTER TABLE guide_version
    ALTER COLUMN game_from DROP NOT NULL,
    ALTER COLUMN game_through DROP NOT NULL,
    ALTER COLUMN game_from_key DROP NOT NULL,
    ALTER COLUMN game_through_key DROP NOT NULL,
    DROP CONSTRAINT guide_web_evidence_valid,
    ADD CONSTRAINT guide_version_complete_game_range
        CHECK (num_nonnulls(game_from, game_through, game_from_key, game_through_key) IN (0, 4)),
    ADD CONSTRAINT guide_web_evidence_valid CHECK (
        (web_evidence IS NULL AND game_from IS NOT NULL) OR COALESCE((
            web_evidence ?& ARRAY['permission', 'parser', 'fields'] AND
            length(web_evidence->>'permission') > 0 AND
            (web_evidence->'fields') ?& ARRAY['entity', 'kind', 'conditions'] AND
            CASE WHEN game_from IS NULL
                THEN web_evidence->>'unknownGameVersionReason' = 'not_stated'
                ELSE (web_evidence->'fields') ? 'versions'
            END
        ), false)
    );
