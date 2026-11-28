-- 只放宽有来源证据的联网攻略；保留既有记录、手工发布限制和版本证据检查。
ALTER TABLE guide_version
    DROP CONSTRAINT guide_version_check1,
    DROP CONSTRAINT guide_web_evidence_valid,
    ADD CONSTRAINT guide_version_publishable_applicability CHECK (
        status <> 'published' OR COALESCE((
            applicability->>'kind' IN ('known', 'unrestricted') OR
            (applicability->>'kind' = 'unknown' AND web_evidence IS NOT NULL
                AND web_evidence->>'parser' = processing_version)
        ), false)
    ),
    ADD CONSTRAINT guide_web_evidence_valid CHECK (
        (web_evidence IS NULL AND game_from IS NOT NULL) OR COALESCE((
            web_evidence ?& ARRAY['permission', 'parser', 'fields'] AND
            length(web_evidence->>'permission') > 0 AND
            (web_evidence->'fields') ?& ARRAY['entity', 'kind'] AND
            CASE WHEN applicability->>'kind' = 'unknown'
                THEN jsonb_typeof(web_evidence->'revision') = 'object'
                    AND jsonb_typeof(web_evidence->'revision'->'id') = 'number'
                    AND jsonb_typeof(web_evidence->'revision'->'section') = 'object'
                    AND (web_evidence->'revision'->'section') ?& ARRAY['index', 'title', 'anchor']
                ELSE (web_evidence->'fields') ? 'conditions'
            END AND
            CASE WHEN game_from IS NULL
                THEN web_evidence->>'unknownGameVersionReason' = 'not_stated'
                ELSE (web_evidence->'fields') ? 'versions'
            END
        ), false)
    );
